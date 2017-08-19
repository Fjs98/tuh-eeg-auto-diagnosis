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
from numpy.random import RandomState

from hyperoptim.parse import cartesian_dict_of_lists_product, \
    product_of_list_of_lists_of_dicts
from hyperoptim.util import save_pkl_artifact, save_npy_artifact
from braindecode.datautil.signal_target import SignalAndTarget
from braindecode.torch_ext.util import np_to_var
from braindecode.torch_ext.util import set_random_seeds
from braindecode.experiments.experiment import Experiment
from braindecode.datautil.iterators import CropsFromTrialsIterator
from braindecode.experiments.monitors import (RuntimeMonitor, LossMonitor,
                                              MisclassMonitor)
from braindecode.experiments.stopcriteria import MaxEpochs
from braindecode.models.shallow_fbcsp import ShallowFBCSPNet
from braindecode.models.deep4 import Deep4Net
from braindecode.models.util import to_dense_prediction_model
from braindecode.datautil.iterators import get_balanced_batches
from braindecode.datautil.splitters import concatenate_sets
from braindecode.torch_ext.constraints import MaxNormDefaultConstraint
from braindecode.torch_ext.util import var_to_np

from autodiag.dataset import DiagnosisSet
from autodiag.sgdr import CosineWithWarmRestarts, ScheduledOptimizer
from autodiag.monitors import compute_preds_per_trial
from autodiag.threepathnet import create_multi_start_path_net
from autodiag.monitors import CroppedDiagnosisMonitor

log = logging.getLogger(__name__)
log.setLevel('DEBUG')

def get_templates():
    return {}


def get_grid_param_list():
    dictlistprod = cartesian_dict_of_lists_product
    default_params = [{
        'save_folder': './data/models/pytorch/auto-diag/final-eval/',
        'only_return_exp': False,
    }]

    save_params = [{
        'save_predictions': False,
        'save_crop_predictions': False,
    }]

    load_params = [{
        'max_recording_mins': 35,
        'n_recordings': 3000,
    }]

    clean_params = [
        {'max_abs_val': 800,
         'shrink_val': None},
    ]

    preproc_params = dictlistprod({
        'sec_to_cut': [60],
        'duration_recording_mins': [20],
        'sampling_freq': [100],
        'divisor': [10],
    })

    # this differentiates train/test also.
    split_params = dictlistprod({
        'test_on_eval': [True],
        'n_folds': [5],
        'i_test_fold': [4],
        'shuffle': [True],
    })

    model_params = [
    # {
    #     'input_time_length': 1200,
    #     'final_conv_length': 35,
    #     'model_name': 'shallow',
    #     'n_start_chans': 40,
    #     'n_chan_factor': None,
    # },
    # {
    #     'input_time_length': 1200,
    #     'final_conv_length': 1,
    #     'model_name': 'deep',
    #     'n_start_chans': 25,
    #     'n_chan_factor': 2,
    # },
    {
        'input_time_length': 6000,
        'final_conv_length': None,
        'model_name': '3path',
        'n_start_chans': None,
        'n_chan_factor': None,
    },
    {
        'input_time_length': 1200,
        'final_conv_length': None,
        'model_name': '3path',
        'n_start_chans': None,
        'n_chan_factor': None,
    },
    ]
    model_constraint_params = dictlistprod({
        'model_constraint': ['defaultnorm',],#None

    })

    adam_params = [
        {
        'sgdr': False,
        'momentum': None,
        'init_lr': 1e-3
    },
    ]

    #sgdr_params = dictlistprod({
        # 'sgdr': [True],
        # 'init_lr': [0.1,0.01],
        # 'momentum': [0.9],
    #})

    optim_params = adam_params #+ sgdr_params

    iterator_params = [{
        'batch_size':  64
    }]

    stop_params = [{
        'max_epochs': 35,
    }]


    grid_params = product_of_list_of_lists_of_dicts([
        default_params,
        save_params,
        load_params,
        clean_params,
        preproc_params,
        split_params,
        model_params,
        optim_params,
        iterator_params,
        stop_params,
        model_constraint_params
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


class TrainValidTestSplitter(object):
    def __init__(self, n_folds, i_test_fold, shuffle):
        self.n_folds = n_folds
        self.i_test_fold = i_test_fold
        self.rng = RandomState(39483948)
        self.shuffle = shuffle

    def split(self, X, y,):
        if len(X) < self.n_folds:
            raise ValueError("Less Trials: {:d} than folds: {:d}".format(
                len(X), self.n_folds
            ))
        folds = get_balanced_batches(len(X), self.rng, self.shuffle,
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


class TrainValidSplitter(object):
    def __init__(self, n_folds, i_valid_fold, shuffle):
        self.n_folds = n_folds
        self.i_valid_fold = i_valid_fold
        self.rng = RandomState(39483948)
        self.shuffle = shuffle

    def split(self, X, y):
        if len(X) < self.n_folds:
            raise ValueError("Less Trials: {:d} than folds: {:d}".format(
                len(X), self.n_folds
            ))
        folds = get_balanced_batches(len(X), self.rng, self.shuffle,
                                     n_batches=self.n_folds)
        valid_inds = folds[self.i_valid_fold]
        all_inds = list(range(len(X)))
        train_inds = np.setdiff1d(all_inds, valid_inds)
        assert np.intersect1d(train_inds, valid_inds).size == 0
        assert np.array_equal(np.sort(np.union1d(train_inds, valid_inds)),
            all_inds)

        train_set = create_set(X, y, train_inds)
        valid_set = create_set(X, y, valid_inds)
        return train_set, valid_set


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


def run_exp(test_on_eval, max_recording_mins, n_recordings,
            sec_to_cut, duration_recording_mins, max_abs_val,
            shrink_val,
            sampling_freq,
            divisor,
            n_folds, i_test_fold,
            shuffle,
            model_name,
            n_start_chans, n_chan_factor,
            input_time_length, final_conv_length,
            model_constraint,
            sgdr,
            init_lr,
            momentum,
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
    if shrink_val is not None:
        preproc_functions.append(lambda data, fs:
                                 (shrink_spikes(
                                     data, shrink_val, 1, 9,), fs))

    preproc_functions.append(lambda data, fs: (resampy.resample(data, fs,
                                                                sampling_freq,
                                                                axis=1,
                                                                filter='kaiser_fast'),
                                               sampling_freq))

    if divisor is not None:
        preproc_functions.append(lambda data, fs: (data / divisor, fs))

    dataset = DiagnosisSet(n_recordings=n_recordings,
                           max_recording_mins=max_recording_mins,
                           preproc_functions=preproc_functions,
                           train_or_eval='train')
    if test_on_eval:
        test_dataset = DiagnosisSet(n_recordings=n_recordings,
                                max_recording_mins=None,
                                preproc_functions=preproc_functions,
                                train_or_eval='eval')
    if not only_return_exp:
        X,y = dataset.load()
        if test_on_eval:
            test_X, test_y = test_dataset.load()
    if not test_on_eval:
        splitter = TrainValidTestSplitter(n_folds, i_test_fold,
                                          shuffle=shuffle)
    else:
        splitter = TrainValidSplitter(n_folds, i_valid_fold=i_test_fold,
                                          shuffle=shuffle)
    if not only_return_exp:
        if not test_on_eval:
            train_set, valid_set, test_set = splitter.split(X, y)
            if sgdr:
                train_set = concatenate_sets([train_set, valid_set])
                # dummy valid set...
                valid_set.X = valid_set.X[:3]
                valid_set.y = valid_set.y[:3]
        else:
            if not sgdr:
                train_set, valid_set = splitter.split(X, y)
            else:
                # dummy valid set...
                train_set = SignalAndTarget(X,y)
                valid_set = SignalAndTarget(X[:3], y[:3])
            test_set = SignalAndTarget(test_X, test_y)
            del test_X, test_y
        del X,y # shouldn't be necessary, but just to make sure
    else:
        train_set = None
        valid_set = None
        test_set = None

    set_random_seeds(seed=20170629, cuda=cuda)
    n_classes = 2
    in_chans = 21
    if model_name == 'shallow':
        model = ShallowFBCSPNet(in_chans=in_chans, n_classes=n_classes,
                                n_filters_time=n_start_chans,
                                n_filters_spat=n_start_chans,
                                input_time_length=input_time_length,
                                final_conv_length=final_conv_length).create_network()
    elif model_name == 'deep':
        model = Deep4Net(in_chans, n_classes,
                         n_filters_time=n_start_chans,
                         n_filters_spat=n_start_chans,
                         input_time_length=input_time_length,
                         n_filters_2 = int(n_start_chans * n_chan_factor),
                         n_filters_3 = int(n_start_chans * (n_chan_factor ** 2.0)),
                         n_filters_4 = int(n_start_chans * (n_chan_factor ** 3.0)),
                         final_conv_length=final_conv_length).create_network()
    elif model_name == '3path':
        virtual_chan_1x1_conv = True
        mean_across_features = False
        drop_prob = 0.5
        n_start_filters = 10
        early_bnorm = False
        n_classifier_filters = 100
        later_kernel_len = 5
        extra_conv_stride = 4
        # dont forget to reset n_preds_per_blabla
        model = create_multi_start_path_net(
            in_chans=in_chans,
            virtual_chan_1x1_conv=virtual_chan_1x1_conv,
            n_start_filters=n_start_filters, early_bnorm=early_bnorm,
            later_kernel_len=later_kernel_len,
            extra_conv_stride=extra_conv_stride,
            mean_across_features=mean_across_features,
            n_classifier_filters=n_classifier_filters, drop_prob=drop_prob)

    if not model_name == '3path':
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
    if model_name == '3path':
        n_preds_per_input = input_time_length // 2
    log.info("{:d} predictions per input/trial".format(n_preds_per_input))
    iterator = CropsFromTrialsIterator(batch_size=batch_size,
                                       input_time_length=input_time_length,
                                       n_preds_per_input=n_preds_per_input)

    if sgdr is False:
        optimizer = optim.Adam(model.parameters(), lr=init_lr)
    else:
        n_batches = sum(
            [1 for _ in iterator.get_batches(train_set, shuffle=False)])
        sgd = optim.SGD(model.parameters(), momentum=momentum,
                              lr=init_lr)
        optimizer = ScheduledOptimizer(CosineWithWarmRestarts(
            sgd, batch_period=max_epochs * n_batches, base_lr=init_lr))

    loss_function = lambda preds, targets: F.nll_loss(
        th.mean(preds, dim=2)[:,:,0], targets)

    if model_constraint is not None:
        model_constraint = MaxNormDefaultConstraint()
    monitors = [LossMonitor(), MisclassMonitor(col_suffix='sample_misclass'),
                CroppedDiagnosisMonitor(input_time_length, n_preds_per_input),
                RuntimeMonitor(),]
    stop_criterion = MaxEpochs(max_epochs)
    batch_modifier = None
    run_after_early_stop = True
    exp = Experiment(model, train_set, valid_set, test_set, iterator,
                     loss_function, optimizer, model_constraint,
                     monitors, stop_criterion,
                     remember_best_column='valid_misclass',
                     run_after_early_stop=run_after_early_stop,
                     batch_modifier=batch_modifier,
                     cuda=cuda)
    if not only_return_exp:
        if not sgdr:
            exp.run()
        else:
            exp.setup_training()
            exp.run_until_early_stop()
    else:
        exp.dataset = dataset
        exp.splitter = splitter
        if test_on_eval:
            exp.test_dataset = test_dataset

    return exp


def save_torch_artifact(ex, obj, filename):
    """Uses tempfile and file lock to safely store a pkl object as artefact"""
    import tempfile
    import fasteners
    log.info("Saving torch artifact")
    with tempfile.NamedTemporaryFile(suffix='.pkl') as tmpfile:
        lockname = tmpfile.name + '.lock'
        file_lock = fasteners.InterProcessLock(lockname)
        file_lock.acquire()
        th.save(obj, open(tmpfile.name, 'wb'))
        ex.add_artifact(tmpfile.name, filename)
        file_lock.release()
    log.info("Saved torch artifact")


def run(ex,
        test_on_eval,
        max_recording_mins, n_recordings,
        sec_to_cut, duration_recording_mins, max_abs_val,
        shrink_val,
        sampling_freq,
        divisor,
        n_folds, i_test_fold,
        shuffle,
        model_name, input_time_length, final_conv_length,
        n_start_chans, n_chan_factor,
        model_constraint,
        sgdr, init_lr, momentum,
        batch_size, max_epochs,
        save_predictions,
        save_crop_predictions,
        only_return_exp):
    kwargs = locals()
    kwargs.pop('ex')
    kwargs.pop('save_predictions')
    kwargs.pop('save_crop_predictions')
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
        if save_predictions:
            for setname in ('train', 'valid', 'test'):
                log.info("Compute and save predictions for {:s}...".format(
                    setname))
                dataset = exp.datasets[setname]
                preds_per_batch = [var_to_np(exp.model(np_to_var(b[0]).cuda()))
                          for b in exp.iterator.get_batches(dataset, shuffle=False)]
                preds_per_trial = compute_preds_per_trial(
                    preds_per_batch, dataset,
                    input_time_length=exp.iterator.input_time_length,
                    n_stride=exp.iterator.n_preds_per_input)
                mean_preds_per_trial = [np.mean(preds, axis=(0, 2)) for preds in
                                            preds_per_trial]
                mean_preds_per_trial = np.array(mean_preds_per_trial)
                log.info("Save trial predictions for {:s}...".format(
                    setname))
                save_npy_artifact(ex, mean_preds_per_trial,
                                  '{:s}_trial_preds.npy'.format(setname))
                if save_crop_predictions:
                    log.info("Save crop predictions for {:s}...".format(
                        setname))
                    save_npy_artifact(ex, preds_per_trial,
                                      '{:s}_crop_preds.npy'.format(setname))

    else:
        return exp
