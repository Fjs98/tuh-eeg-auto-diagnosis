import os

os.sys.path.insert(0, '/home/schirrmr/braindecode/code/')
os.sys.path.insert(0, '/home/schirrmr/braindecode/code/braindecode/')
os.sys.path.insert(0, '/home/schirrmr/code/auto-diagnosis/')
import logging
import time

import numpy as np
import resampy
from torch import optim
import torch.nn.functional as F
import torch as th

from hyperoptim.parse import cartesian_dict_of_lists_product, \
    product_of_list_of_lists_of_dicts
from hyperoptim.util import save_pkl_artifact
from braindecode.datautil.signalproc import (exponential_running_standardize,
    exponential_running_demean)
from braindecode.datautil.signal_target import SignalAndTarget
from braindecode.torch_ext.util import np_to_var
from braindecode.torch_ext.util import set_random_seeds
from braindecode.experiments.experiment import Experiment
from braindecode.datautil.iterators import CropsFromTrialsIterator
from braindecode.experiments.monitors import (RuntimeMonitor, LossMonitor,
                                              CroppedTrialMisclassMonitor,
                                              MisclassMonitor)
from braindecode.experiments.stopcriteria import MaxEpochs
from braindecode.models.shallow_fbcsp import ShallowFBCSPNet
from braindecode.models.deep4 import Deep4Net
from braindecode.models.util import to_dense_prediction_model
from braindecode.datautil.iterators import get_balanced_batches
from braindecode.datautil.signalproc import bandpass_cnt

from autodiag.dataset import load_data, DiagnosisSet
from autodiag.clean import set_jumps_to_zero
from autodiag.batch_modifier import RemoveMinMaxDiff
from autodiag.iterator import ModifiedIterator
from autodiag.modelutil import to_linear_plus_minus_net
from autodiag.losses import binary_cross_entropy_with_logits

log = logging.getLogger(__name__)
log.setLevel('DEBUG')

def get_templates():
    return {}


def get_grid_param_list():
    dictlistprod = cartesian_dict_of_lists_product
    default_params = [{
        'save_folder': './data/models/pytorch/auto-diag/mean-before-after-softmax/',
        'only_return_exp': False,
    }]

    load_params = [{
        'max_recording_mins': 35,
        'n_recordings': 1500,
    }]

    clean_defaults = {
        'max_min_threshold': None,
        'shrink_val': None,
        'max_min_expected': None,
        'max_abs_val': None,
        'batch_set_zero_val': None,
        'batch_set_zero_test': None,
        'max_min_remove': None,
    }

    clean_variants = [
        #{},
        #{'batch_set_zero_val': 500, 'batch_set_zero_test': True},
        {'max_abs_val' : 800},
        #{'max_abs_val' : 500},
        #{'shrink_val': 200},
        #{'shrink_val': 500},
        #{'shrink_val': 800},
    ]

    clean_params = product_of_list_of_lists_of_dicts(
        [[clean_defaults], clean_variants])

    preproc_params = dictlistprod({
        'sec_to_cut': [60],
        'duration_recording_mins': [3],
        'sampling_freq': [100],
        'low_cut_hz': [None,],
        'high_cut_hz': [None,],
        'divisor': [10],
    })

    standardizing_defaults = {
        'exp_demean': False,
        'exp_standardize': False,
        'moving_demean': False,
        'moving_standardize': False,
        'channel_demean': False,
        'channel_standardize': False,
    }

    standardizing_variants = [
        {},
    ]

    standardizing_params = product_of_list_of_lists_of_dicts(
        [[standardizing_defaults], standardizing_variants])

    split_params = dictlistprod({
        'n_folds': [5],
        'i_test_fold': [0,1,2,3,4],
    })

    model_params = [
    {
        'input_time_length': 1200,
        'final_conv_length': 40,
        'model_name': 'shallow',
    },
    {
        'input_time_length': 1200,
        'final_conv_length': 2,
        'model_name': 'deep',
    },
    ]

    final_layer_params = dictlistprod({
        'mean_before_softmax': [True, False]
    })

    iterator_params = [{
        'batch_size':  64
    }]

    stop_params = [{
        'max_epochs': 35,
    }]



    grid_params = product_of_list_of_lists_of_dicts([
        default_params,
        load_params,
        clean_params,
        preproc_params,
        split_params,
        model_params,
        final_layer_params,
        iterator_params,
        standardizing_params,
        stop_params
    ])

    return grid_params


def sample_config_params(rng, params):
    return params

def create_set(X, y, inds):
    """
    X list and y nparray
    :return: 
    """
    new_X = []
    for i in inds:
        new_X.append(X[i])
    new_y = y[inds]
    return SignalAndTarget(new_X, new_y)


class Splitter(object):
    def __init__(self, n_folds, i_test_fold):
        self.n_folds = n_folds
        self.i_test_fold = i_test_fold

    def split(self, X, y):
        folds = get_balanced_batches(len(X), None, False,
                                     n_batches=self.n_folds)
        test_inds = folds[self.i_test_fold]
        valid_inds = folds[self.i_test_fold - 1]
        all_inds = list(range(len(X)))
        train_inds = np.setdiff1d(all_inds, np.union1d(test_inds, valid_inds))
        assert np.intersect1d(train_inds, valid_inds).size == 0
        assert np.intersect1d(train_inds, test_inds).size == 0
        assert np.intersect1d(valid_inds, test_inds).size == 0
        assert np.array_equal(np.sort(
            np.union1d(train_inds, np.union1d(valid_inds, test_inds))),
            all_inds)

        train_set = create_set(X, y, train_inds)
        valid_set = create_set(X, y, valid_inds)
        test_set = create_set(X, y, test_inds)

        return train_set, valid_set, test_set


def running_mean(arr, window_len, axis=0):
    # adapted from http://stackoverflow.com/a/27681394/1469195
    # need to pad to get correct first value also
    arr_padded = np.insert(arr,0,values=0,axis=axis)
    cumsum = np.cumsum(arr_padded,axis=axis)
    later_sums = np.take(cumsum, range(window_len, arr_padded.shape[axis]),
        axis=axis)
    earlier_sums = np.take(cumsum, range(0, arr_padded.shape[axis] - window_len),
        axis=axis)

    moving_average = (later_sums - earlier_sums) / float(window_len)
    return moving_average


def padded_moving_mean(arr, axis, n_window):
    """Pads by replicating n_window first and last elements
    and putting them at end and start (no reflection)"""
    start_pad_inds = list(range(0, n_window // 2))
    end_pad_inds = list(range(arr.shape[axis] - (n_window // 2),
                              arr.shape[axis]))
    arr = np.concatenate((arr.take(start_pad_inds, axis=axis),
                          arr,
                          arr.take(end_pad_inds, axis=axis)),
                         axis=axis)
    mov_mean = running_mean(arr, window_len=n_window, axis=axis)
    return mov_mean


def padded_moving_demean(arr, axis, n_window):
    assert arr.dtype != np.float16
    assert n_window % 2 == 1
    mov_mean = padded_moving_mean(arr, axis, n_window=n_window)
    arr = arr - mov_mean
    return arr


def padded_moving_divide_square(arr, axis, n_window, eps=0.1):
    """Pads by replicating n_window first and last elements
    and putting them at end and start (no reflection)"""
    assert arr.dtype != np.float16
    assert n_window % 2 == 1
    mov_mean_sqr = padded_moving_mean(np.square(arr), axis=axis, n_window=n_window)
    arr = arr / np.maximum(np.sqrt(mov_mean_sqr), eps)
    return arr


def padded_moving_standardize(arr, axis, n_window, eps=0.1):
    assert arr.dtype != np.float16
    assert n_window % 2 == 1
    arr = padded_moving_demean(arr, axis=axis, n_window=n_window)
    arr = padded_moving_divide_square(arr, axis=axis, n_window=n_window, eps=eps)
    return arr


def demean(x, axis):
    return x - np.mean(x, axis=axis, keepdims=True)

def standardize(x, axis):
    return (x - np.mean(x, axis=axis, keepdims=True)) / np.maximum(
        1e-4, np.std(x, axis=axis, keepdims=True))


def clean_jumps(x, window_len, threshold, expected, cuda):
    x_var = np_to_var([x])
    if cuda:
        x_var = x_var.cuda()

    maxs = F.max_pool1d(x_var,window_len, stride=1)
    mins = F.max_pool1d(-x_var,window_len, stride=1)

    diffs = maxs + mins
    large_diffs = (diffs > threshold).type_as(diffs) * diffs
    padded = F.pad(large_diffs.unsqueeze(0), (window_len-1,window_len-1, 0,0), 'constant', 0)
    max_diffs = th.max(padded[:,:,:,window_len-1:], padded[:,:,:,:-window_len+1]).unsqueeze(0)
    max_diffs = th.clamp(max_diffs, min=expected)
    x_var = x_var * (expected / max_diffs)

    x = x_var.data.cpu().numpy()[0]
    return x



def shrink_spikes(example, threshold, axis, n_window):
    """Example could be single example or all...
    should work for both."""
    run_mean = padded_moving_mean(example.astype(np.float32),
        axis=axis, n_window=n_window)
    abs_run_mean = np.abs(run_mean)
    is_relevant = (abs_run_mean > threshold)

    cleaned_example = example - is_relevant * (run_mean - (
             np.sign(run_mean) * (threshold +
            np.log(np.maximum(abs_run_mean - threshold + 1, 0.01)))))
    return cleaned_example

from braindecode.torch_ext.modules import Expression
from torch import nn


def to_mean_before_softmax(model):
    model_meaned = nn.Sequential()
    for name, module in model.named_children():
        if name == 'softmax':
            model_meaned.add_module('mean_outputs', Expression(lambda x: th.mean(x, dim=2)))
        model_meaned.add_module(name, module)
    return model_meaned

def to_mean_after_softmax(model):
    model_meaned = nn.Sequential()
    for name, module in model.named_children():
        model_meaned.add_module(name, module)
        if name == 'softmax':
            model_meaned.add_module('mean_outputs', Expression(lambda x: th.mean(x, dim=2)))
    return model_meaned


def run_exp(max_recording_mins, n_recordings,
            sec_to_cut, duration_recording_mins, max_abs_val,
            max_min_threshold, max_min_expected, shrink_val,
            max_min_remove, batch_set_zero_val, batch_set_zero_test,
            sampling_freq,
            low_cut_hz, high_cut_hz,
            exp_demean, exp_standardize,
            moving_demean, moving_standardize,
            channel_demean, channel_standardize,
            divisor,
            n_folds, i_test_fold,
            model_name,
            input_time_length, final_conv_length,
            mean_before_softmax,
            batch_size, max_epochs,
            only_return_exp):
    cuda = True

    preproc_functions = []
    preproc_functions.append(
        lambda data, fs: (data[:, int(sec_to_cut * fs):-int(
            sec_to_cut * fs)], fs))
    preproc_functions.append(
        lambda data, fs: (data[:, :int(duration_recording_mins * 60 * fs)], fs))
    if max_abs_val is not None:
        preproc_functions.append(lambda data, fs:
                                 (np.clip(data, -max_abs_val, max_abs_val), fs))
    if max_min_threshold is not None:
        preproc_functions.append(lambda data, fs:
                                 (clean_jumps(
                                     data, 200, max_min_threshold,
                                     max_min_expected, cuda), fs))
    if max_min_remove is not None:
        window_len = 200
        preproc_functions.append(lambda data, fs:
                                 (set_jumps_to_zero(
                                     data, window_len=window_len,
                                     threshold=max_min_remove,
                                     cuda=cuda,
                                     clip_min_max_to_zero=True), fs))

    if shrink_val is not None:
        preproc_functions.append(lambda data, fs:
                                 (shrink_spikes(
                                     data, shrink_val, 1, 9,), fs))

    preproc_functions.append(lambda data, fs: (resampy.resample(data, fs,
                                                                sampling_freq,
                                                                axis=1,
                                                                filter='kaiser_fast'),
                                               sampling_freq))
    preproc_functions.append(lambda data, fs:
                             (bandpass_cnt(data, low_cut_hz, high_cut_hz,
                                           fs,
                                           filt_order=4, axis=1),
                              fs))

    if exp_demean:
        preproc_functions.append(lambda data, fs: (exponential_running_demean(
            data.T, factor_new=0.001, init_block_size=100).T, fs))
    if exp_standardize:
        preproc_functions.append(lambda data, fs: (exponential_running_standardize(
            data.T, factor_new=0.001, init_block_size=100).T, fs))
    if moving_demean:
        preproc_functions.append(lambda data, fs: (padded_moving_demean(
            data, axis=1, n_window=201), fs))
    if moving_standardize:
        preproc_functions.append(lambda data, fs: (padded_moving_standardize(
            data, axis=1, n_window=201), fs))
    if channel_demean:
        preproc_functions.append(lambda data, fs: (demean(data, axis=1), fs))
    if channel_standardize:
        preproc_functions.append(lambda data, fs: (standardize(data, axis=1), fs))
    if divisor is not None:
        preproc_functions.append(lambda data, fs: (data / divisor, fs))

    dataset = DiagnosisSet(n_recordings=n_recordings,
                        max_recording_mins=max_recording_mins,
                        preproc_functions=preproc_functions)
    if not only_return_exp:
        X,y = dataset.load()

    splitter = Splitter(n_folds, i_test_fold,)
    if not only_return_exp:
        train_set, valid_set, test_set = splitter.split(X,y)
        del X,y # shouldn't be necessary, but just to make sure
    else:
        train_set = None
        valid_set = None
        test_set = None

    set_random_seeds(seed=20170629, cuda=cuda)
    in_chans = 21
    n_classes = 2
    if model_name == 'shallow':
        model = ShallowFBCSPNet(in_chans=in_chans, n_classes=n_classes,
                                input_time_length=input_time_length,
                                final_conv_length=final_conv_length).create_network()
    elif model_name == 'deep':
        model = Deep4Net(in_chans, n_classes, input_time_length=input_time_length,
                 final_conv_length=final_conv_length).create_network()
    optimizer = optim.Adam(model.parameters())
    to_dense_prediction_model(model)
    log.info("Model:\n{:s}".format(str(model)))
    if cuda:
        model.cuda()
    # determine output size
    test_input = np_to_var(
        np.ones((2, in_chans, input_time_length, 1), dtype=np.float32))
    if cuda:
        test_input = test_input.cuda()
    out = model(test_input)
    n_preds_per_input = out.cpu().data.numpy().shape[2]

    if mean_before_softmax:
        model = to_mean_before_softmax(model)
    else:
        model = to_mean_after_softmax(model)

    log.info("{:d} predictions per input/trial".format(n_preds_per_input))
    iterator = CropsFromTrialsIterator(batch_size=batch_size,
                                       input_time_length=input_time_length,
                                      n_preds_per_input=n_preds_per_input)
    loss_function = F.nll_loss

    model_constraint = None
    monitors = [LossMonitor(), MisclassMonitor(col_suffix='misclass'),
                RuntimeMonitor(),]
    stop_criterion = MaxEpochs(max_epochs)
    batch_modifier = None
    if batch_set_zero_val is not None:
        batch_modifier = RemoveMinMaxDiff(batch_set_zero_val, clip_max_abs=True,
                                          set_zero=True)
    if (batch_set_zero_val is not None) and (batch_set_zero_test == True):
        iterator = ModifiedIterator(iterator, batch_modifier,)
        batch_modifier = None
    exp = Experiment(model, train_set, valid_set, test_set, iterator,
                     loss_function, optimizer, model_constraint,
                     monitors, stop_criterion,
                     remember_best_column='valid_misclass',
                     run_after_early_stop=True, batch_modifier=batch_modifier,
                     cuda=cuda)
    if not only_return_exp:
        exp.run()
    else:
        exp.dataset = dataset
        exp.splitter = splitter

    return exp


def save_torch_artifact(ex, obj, filename):
    """Uses tempfile and file lock to safely store a pkl object as artefact"""
    import tempfile
    import fasteners
    with tempfile.NamedTemporaryFile(suffix='.pkl') as tmpfile:
        lockname = tmpfile.name + '.lock'
        file_lock = fasteners.InterProcessLock(lockname)
        file_lock.acquire()
        th.save(obj, open(tmpfile.name, 'wb'))
        ex.add_artifact(tmpfile.name, filename)
        file_lock.release()

def run(ex, max_recording_mins, n_recordings,
        sec_to_cut, duration_recording_mins, max_abs_val,
        max_min_threshold, max_min_expected, shrink_val,
        max_min_remove, batch_set_zero_val, batch_set_zero_test,
        sampling_freq,
        low_cut_hz, high_cut_hz,
        exp_demean, exp_standardize,
        moving_demean, moving_standardize,
        channel_demean, channel_standardize,
        divisor,
        n_folds, i_test_fold,
        model_name, input_time_length, final_conv_length,
        mean_before_softmax,
        batch_size, max_epochs,
        only_return_exp):
    kwargs = locals()
    kwargs.pop('ex')
    import sys
    logging.basicConfig(format='%(asctime)s %(levelname)s : %(message)s',
                     level=logging.DEBUG, stream=sys.stdout)
    start_time = time.time()
    ex.info['finished'] = False

    exp = run_exp(**kwargs)
    end_time = time.time()
    run_time = end_time - start_time
    ex.info['finished'] = True

    if not only_return_exp:
        last_row = exp.epochs_df.iloc[-1]
        for key, val in last_row.iteritems():
            ex.info[key] = float(val)
    ex.info['runtime'] = run_time
    if not only_return_exp:
        save_pkl_artifact(ex, exp.epochs_df, 'epochs_df.pkl')
        save_pkl_artifact(ex, exp.before_stop_df, 'before_stop_df.pkl')
        save_torch_artifact(ex, exp.model.state_dict(), 'model_params.pkl')
