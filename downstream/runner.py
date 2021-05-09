import os
import math
import glob
import random
import importlib
import pandas as pd
from pathlib import Path
from collections import defaultdict

import torch
import torch.nn as nn
import numpy as np
from tqdm import tqdm
from tensorboardX import SummaryWriter

from optimizers import get_optimizer
from schedulers import get_scheduler
from utility.helper import count_parameters
from sklearn.metrics import classification_report
from pprint import pprint

SAMPLE_RATE = 16000


class Runner():
    """
    Used to handle high-level concepts of a ML experiment
    eg. training loop, evaluation loop, upstream propagation, optimization, tensorboard logging, checkpoint saving
    """
    def __init__(self, args, config):
        self.args = args
        self.config = config
        self.logger = SummaryWriter(args.expdir)

        self.init_ckpt = torch.load(self.args.past_exp, map_location='cpu') if self.args.past_exp else {}
        self.upstream = self._get_upstream()
        self.downstream = self._get_downstream()
        #getattr(importlib.import_module(f'downstream.{self.args.downstream}.dataset'), 'CLASSES') 
        self.classes = getattr(importlib.import_module(f'downstream.{self.args.downstream}.dataset'), 'CLASSES')
        #print(self.classes)
        # set up the downstream name used by Tensorboard
        self.downstream_name = self.args.downstream
        if hasattr(self.downstream, 'get_downstream_name'):
            self.downstream_name = self.downstream.get_downstream_name()


    def _get_upstream(self):
        Upstream = getattr(importlib.import_module('hubconf'), self.args.upstream)
        upstream = Upstream(
            feature_selection = self.args.upstream_feature_selection,
            model_config = self.args.upstream_model_config,
            refresh = self.args.upstream_refresh,
            ckpt = self.args.upstream_ckpt,
        ).to(self.args.device)

        assert hasattr(upstream, 'forward')
        assert hasattr(upstream, 'get_output_dim')
        assert hasattr(upstream, 'get_downsample_rate')

        print(f'[Runner] - Upstream model architecture: {upstream}')
        print(f'[Runner] - Upstream output dimension: {upstream.get_output_dim()}')
        downsample = upstream.get_downsample_rate()
        print(f'[Runner] - Upstream downsample rate: {downsample} ({downsample / SAMPLE_RATE * 1000} ms/frame)')

        init_upstream = self.init_ckpt.get('Upstream')
        if init_upstream:
            print('[Runner] - Loading upstream weights from the previous experiment')
            upstream.load_state_dict(init_upstream)
        return upstream


    def _get_downstream(self):
        module_path = f'downstream.{self.args.downstream}.expert'
        Downstream = getattr(importlib.import_module(module_path), 'DownstreamExpert')
        downstream = Downstream(
            upstream_dim = self.upstream.get_output_dim(),
            **self.config,
            **vars(self.args)
        ).to(self.args.device)

        print(f'[Runner] - Downstream model architecture: {downstream}')
        print(f'[Runner] - Downstream has {count_parameters(downstream)} parameters')

        assert hasattr(downstream, 'get_train_dataloader')
        assert hasattr(downstream, 'get_dev_dataloader')
        assert hasattr(downstream, 'get_test_dataloader')
        assert hasattr(downstream, 'forward')
        assert hasattr(downstream, 'log_records')

        init_downstream = self.init_ckpt.get('Downstream')
        if init_downstream:
            print('[Runner] - Loading downstream weights from the previous experiment')
            downstream.load_state_dict(init_downstream)
        return downstream


    def _get_optimizer(self, model_params):
        optimizer = get_optimizer(
            model_params, 
            self.config['runner']['total_steps'],
            self.config['optimizer']
        )

        init_optimizer = self.init_ckpt.get('Optimizer')
        if init_optimizer:
            print('[Runner] - Loading optimizer weights from the previous experiment')
            optimizer.load_state_dict(init_optimizer)
        return optimizer


    def _get_scheduler(self, optimizer):
        scheduler = get_scheduler(
            optimizer,
            self.config['runner']['total_steps'],
            self.config['scheduler']
        )

        init_scheduler = self.init_ckpt.get('Scheduler')
        if init_scheduler:
            print('[Runner] - Loading scheduler weights from the previous experiment')
            scheduler.load_state_dict(init_scheduler)
        return scheduler


    def train(self):
        # set model train/eval modes
        self.downstream.train()
        self.upstream.eval()
        if self.args.upstream_trainable:
            self.upstream.train()

        # set optimizer
        model_params = [self.downstream]
        if self.args.upstream_trainable:
            model_params.append(self.upstream)
        optimizer = self._get_optimizer(model_params)

        # set scheduler
        scheduler = None
        if self.config.get('scheduler'):
            scheduler = self._get_scheduler(optimizer)

        # set progress bar
        pbar = tqdm(total=self.config['runner']['total_steps'], dynamic_ncols=True, desc='overall')
        init_step = self.init_ckpt.get('Step')
        if init_step:
            pbar.n = init_step

        # prepare data
        dataloader = self.downstream.get_train_dataloader()

        all_loss = []
        backward_steps = 0
        records = defaultdict(list)
        prefix = f'{self.downstream_name}/train-'

        while pbar.n < pbar.total:
            for batch_id, (wavs, *others) in enumerate(tqdm(dataloader, dynamic_ncols=True, desc='train')):
                # try/except block for forward/backward
                try:
                    if pbar.n >= pbar.total:
                        break
                    global_step = pbar.n + 1

                    wavs = [wav.to(self.args.device) for wav in wavs]
                    if self.args.upstream_trainable:
                        try:
                            features = self.upstream(wavs)
                        except RuntimeError:
                            print("Run time error")
                            continue    
                    else:
                        with torch.no_grad():
                            features = self.upstream(wavs)

                    loss,_,_,_ = self.downstream(
                        features, *others,
                        records = records,
                        logger = self.logger,
                        prefix = prefix,
                        global_step = global_step,
                        log_step = self.config['runner']['log_step'],
                        batch_id = batch_id,
                        batch_num = len(dataloader),
                    )
                    gradient_accumulate_steps = self.config['runner'].get('gradient_accumulate_steps')
                    (loss / gradient_accumulate_steps).backward()

                except RuntimeError as e:
                    if 'CUDA out of memory' in str(e):
                        print(f'[Runner] - CUDA out of memory at step {global_step}')
                        with torch.cuda.device(self.args.device):
                            torch.cuda.empty_cache()
                        optimizer.zero_grad()
                        continue
                    else:
                        raise

                # record loss
                all_loss.append(loss.item())
                del loss
                
                # whether to accumulate gradient
                backward_steps += 1
                if backward_steps % gradient_accumulate_steps > 0:
                    continue

                # gradient clipping
                paras = list(self.downstream.parameters())
                if self.args.upstream_trainable:
                    paras += list(self.upstream.parameters())
                grad_norm = torch.nn.utils.clip_grad_norm_(paras, self.config['runner']['gradient_clipping'])

                # optimize
                if math.isnan(grad_norm):
                    print(f'[Runner] - grad norm is NaN at step {global_step}')
                else:
                    optimizer.step()
                optimizer.zero_grad()

                # adjust learning rate
                if scheduler:
                    scheduler.step()

                # logging
                if global_step % self.config['runner']['log_step'] == 0:
                    # log loss
                    average_loss = torch.FloatTensor(all_loss).mean().item()
                    self.logger.add_scalar(f'{prefix}loss', average_loss, global_step=global_step)
                    all_loss = []

                    # log customized contents
                    self.downstream.log_records(
                        records = records,
                        logger = self.logger,
                        prefix = prefix,
                        global_step = global_step,
                        log_step = self.config['runner']['log_step'],
                    )
                    records = defaultdict(list)

                # evaluation and save checkpoint
                save_names = []

                if global_step % self.config['runner']['eval_step'] == 0:
                    for split in self.config['runner']['eval_dataloaders']:
                        save_names += self.evaluate(split, global_step)

                if global_step % self.config['runner']['save_step'] == 0:
                    def check_ckpt_num(directory):
                        max_keep = self.config['runner']['max_keep']
                        ckpt_pths = glob.glob(f'{directory}/states-*.ckpt')
                        if len(ckpt_pths) >= max_keep:
                            ckpt_pths = sorted(ckpt_pths, key=lambda pth: int(pth.split('-')[-1].split('.')[0]))
                            for ckpt_pth in ckpt_pths[:len(ckpt_pths) - max_keep + 1]:
                                os.remove(ckpt_pth)
                    check_ckpt_num(self.args.expdir)
                    save_names.append(f'states-{global_step}.ckpt')

                if len(save_names) > 0:
                    all_states = {
                        'Downstream': self.downstream.state_dict(),
                        'Optimizer': optimizer.state_dict(),
                        'Step': global_step,
                        'Args': self.args,
                        'Config': self.config,
                    }

                    if scheduler:
                        all_states['Scheduler'] = scheduler.state_dict()

                    if self.args.upstream_trainable:
                        all_states['Upstream'] = self.upstream.state_dict()

                    save_paths = [os.path.join(self.args.expdir, name) for name in save_names]
                    tqdm.write(f'[Runner] - Save the checkpoint to:')
                    for i, path in enumerate(save_paths):
                        tqdm.write(f'{i + 1}. {path}')
                        torch.save(all_states, path)

                pbar.update(1)

        Path(f'{self.args.expdir}/train_finished').touch(exist_ok=True)
        pbar.close()


    def evaluate(self, split='test', global_step=0):
        # fix seed to guarantee the same evaluation protocol across steps 
        random.seed(self.args.seed)
        np.random.seed(self.args.seed)
        torch.manual_seed(self.args.seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(self.args.seed)
        with torch.cuda.device(self.args.device):
            torch.cuda.empty_cache()

        # record original train/eval states and set all models to eval
        downstream_training = self.downstream.training
        upstream_training = self.upstream.training
        self.downstream.eval()
        self.upstream.eval()

        # prepare data
        dataloader = eval(f'self.downstream.get_{split}_dataloader')()

        # main evaluation block
        all_loss = []
        all_labels = []
        all_predictions = []
        all_confidence_scores = []
        all_audio_paths = []
        records = defaultdict(list)
        prefix = f'{self.downstream_name}/{split}-'

        for batch_id, (wavs, *others) in enumerate(tqdm(dataloader, dynamic_ncols=True, desc=split)):

            wavs = [wav.to(self.args.device) for wav in wavs]
            with torch.no_grad():
                features = self.upstream(wavs)

                loss, predicted_classid,labels,audio_paths,confidence_scores = self.downstream(
                    features, *others,
                    records = records,
                    logger = self.logger,
                    prefix = prefix,
                    global_step = global_step,
                    log_step = self.config['runner']['log_step'],
                    batch_id = batch_id,
                    batch_num = len(dataloader),
                )
                all_loss.append(loss.item())
                all_labels.extend(labels)
                all_predictions.extend(predicted_classid)
                all_confidence_scores.extend(confidence_scores[0])
                all_audio_paths.extend(audio_paths)
        #Print sklearn classification report
        pprint(classification_report(all_labels,all_predictions,target_names=self.classes))
        # log loss
        average_loss = torch.FloatTensor(all_loss).mean().item()
        self.logger.add_scalar(f'{prefix}loss', average_loss, global_step=global_step)
        all_loss = []

        errors = []
        for i, (pred_label, true_label) in enumerate(zip(all_predictions, all_labels)):
            if pred_label != true_label:
                errors.append(
                    [
                        all_audio_paths[i],
                        true_label, 
                        pred_label,
                    ]
                )
        errors = pd.DataFrame(errors, columns=['audio_path', 'true_label', 'pred_label'])
        errors.to_csv(self.args.expdir + '/errors.csv')

        # log customized contents
        save_names = self.downstream.log_records(
            records = records,
            logger = self.logger,
            prefix = prefix,
            global_step = global_step,
            log_step = self.config['runner']['log_step'],
        )
        records = defaultdict(list)

        # prepare back to training
        with torch.cuda.device(self.args.device):
            torch.cuda.empty_cache()
        if downstream_training:
            self.downstream.train()
        if upstream_training:
            self.upstream.train()

        return [] if type(save_names) is not list else save_names
