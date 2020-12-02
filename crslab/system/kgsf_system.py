# @Time   : 2020/11/22
# @Author : Kun Zhou
# @Email  : francis_kun_zhou@163.com

# UPDATE:
# @Time   : 2020/11/24, 2020/12/1
# @Author : Kun Zhou, Xiaolei Wang
# @Email  : francis_kun_zhou@163.com, wxl1999@foxmail.com

import torch
from loguru import logger
from tqdm import tqdm

from crslab.evaluator.gen_metrics import PPLMetric
from crslab.evaluator.metrics import AverageMetric
from crslab.system.base_system import BaseSystem


class KGSFSystem(BaseSystem):
    r"""S3RecTrainer is designed for S3Rec, which is a self-supervised learning based sequentail recommenders.
        It includes two training stages: pre-training ang fine-tuning.
    """

    def __init__(self, opt, train_dataloader, valid_dataloader, test_dataloader, ind2tok, side_data):
        super(KGSFSystem, self).__init__(opt, train_dataloader, valid_dataloader, test_dataloader, ind2tok, side_data)

        self.movie_ids = side_data['item_entity_ids']

        self.pretrain_optim_opt = self.opt['pretrain']
        self.rec_optim_opt = self.opt['rec']
        self.conv_optim_opt = self.opt['conv']
        self.pretrain_epoch = self.opt['pretrain']['epoch']
        self.rec_epoch = self.opt['rec']['epoch']
        self.conv_epoch = self.opt['conv']['epoch']
        self.pretrain_batch_size = self.opt['batch_size']['pretrain']
        self.rec_batch_size = self.opt['batch_size']['rec']
        self.conv_batch_size = self.opt['batch_size']['conv']

    def rec_evaluate(self, rec_predict, movie_label):
        rec_predict = rec_predict.cpu().detach()
        rec_predict = rec_predict[:, self.movie_ids]
        _, rec_ranks = torch.topk(rec_predict, 50, dim=-1)
        movie_label = movie_label.cpu().detach()
        for rec_rank, movie in zip(rec_ranks, movie_label):
            movie = self.movie_ids.index(movie.item())
            self.evaluator.rec_evaluate(rec_rank, movie)

    def conv_evaluate(self, prediction, response):
        prediction = prediction.cpu().detach()
        response = response.cpu().detach()
        for p, r in zip(prediction, response):
            p_str = self.ind2txt(p)
            r_str = self.ind2txt(r)
            self.evaluator.gen_evaluate(p_str, [r_str])

    def step(self, batch, stage, mode):
        """
        stage: ['pretrain', 'rec', 'conv']
        mode: ['train', 'val', 'test]
        """
        batch = [ele.to(self.device) for ele in batch]
        if stage == 'pretrain':
            info_loss = self.model.pretrain_infomax(batch)
            if info_loss:
                self.backward(info_loss)
                info_loss = info_loss.item()
                self.evaluator.optim_metrics.add("info_loss", AverageMetric(info_loss))
        elif stage == 'rec':
            rec_loss, info_loss, rec_predict = self.model.recommender(batch, mode)
            if info_loss:
                loss = rec_loss + 0.025 * info_loss
            else:
                loss = rec_loss
            if mode == "train":
                self.backward(loss)
            else:
                self.rec_evaluate(rec_predict, batch[-1])
            rec_loss = rec_loss.item()
            self.evaluator.optim_metrics.add("rec_loss", AverageMetric(rec_loss))
            if info_loss:
                info_loss = info_loss.item()
                self.evaluator.optim_metrics.add("info_loss", AverageMetric(info_loss))
        elif stage == "conv":
            if mode != "test":
                gen_loss, pred = self.model.conversation(batch, mode)
                if mode == 'train':
                    self.backward(gen_loss)
                gen_loss = gen_loss.item()
                self.evaluator.optim_metrics.add("gen_loss", AverageMetric(gen_loss))
                self.evaluator.gen_metrics.add("ppl", PPLMetric(gen_loss))
            else:
                pred = self.model.conversation(batch, mode)
            self.conv_evaluate(pred, batch[-1])
        else:
            raise

    def pretrain(self, debug=False):
        if debug:
            train_dataloader = self.valid_dataloader
        else:
            train_dataloader = self.train_dataloader

        self.build_optimizer(self.pretrain_optim_opt, self.model.parameters())
        self.build_lr_scheduler(self.pretrain_optim_opt)

        for epoch in range(self.pretrain_epoch):
            self.evaluator.reset_metrics()
            logger.info(f'[Pretrain epoch {str(epoch)}]')
            for batch in train_dataloader.get_pretrain_data(self.pretrain_batch_size, shuffle=False):
                self.step(batch, stage="pretrain", mode='train')
            self.evaluator.report()

    def train_recommender(self, debug=False):
        if debug:
            train_dataloader = self.valid_dataloader
            valid_dataloader = self.valid_dataloader
            test_dataloader = self.test_dataloader
        else:
            train_dataloader = self.train_dataloader
            valid_dataloader = self.valid_dataloader
            test_dataloader = self.test_dataloader

        self.build_optimizer(self.rec_optim_opt, self.model.parameters())
        self.build_lr_scheduler(self.rec_optim_opt)

        for epoch in range(self.rec_epoch):
            self.evaluator.reset_metrics()
            logger.info(f'[Recommendation epoch {str(epoch)}]')
            for batch in train_dataloader.get_rec_data(self.rec_batch_size, shuffle=False):
                self.step(batch, stage='rec', mode='train')
            self.evaluator.report()
            # val
            with torch.no_grad():
                self.evaluator.reset_metrics()
                for batch in valid_dataloader.get_rec_data(self.rec_batch_size, shuffle=False):
                    self.step(batch, stage='rec', mode='val')
                self.evaluator.report()
                # early stop
                metric = self.evaluator.rec_metrics['recall@1'] + self.evaluator.rec_metrics['recall@50']
                self.early_stop(metric)
                if self.stop:
                    break
        # test
        with torch.no_grad():
            self.evaluator.reset_metrics()
            for batch in test_dataloader.get_rec_data(self.rec_batch_size, shuffle=False):
                self.step(batch, stage='rec', mode='test')
            self.evaluator.report()

    def train_conversation(self, debug=False):
        if debug:
            train_dataloader = self.valid_dataloader
            valid_dataloader = self.valid_dataloader
            test_dataloader = self.test_dataloader
        else:
            train_dataloader = self.train_dataloader
            valid_dataloader = self.valid_dataloader
            test_dataloader = self.test_dataloader

        self.model.stem_conv_parameters()
        self.build_optimizer(self.conv_optim_opt, self.model.parameters())
        self.build_lr_scheduler(self.conv_optim_opt)

        for epoch in range(self.conv_epoch):
            self.evaluator.reset_metrics()
            logger.info(f'[Conversation epoch {str(epoch)}]')
            for batch in train_dataloader.get_conv_data(batch_size=self.conv_batch_size, shuffle=False):
                self.step(batch, stage='conv', mode='train')
            self.evaluator.report()
            # val
            with torch.no_grad():
                self.evaluator.reset_metrics()
                for batch in valid_dataloader.get_conv_data(batch_size=self.conv_batch_size, shuffle=False):
                    self.step(batch, stage='conv', mode='val')
                self.evaluator.report()
        # test
        with torch.no_grad():
            self.evaluator.reset_metrics()
            for batch in test_dataloader.get_conv_data(batch_size=self.conv_batch_size, shuffle=False):
                self.step(batch, stage='conv', mode='test')
            self.evaluator.report()

    def fit(self, debug=False):
        r"""Train the model based on the train data.

        """
        self.pretrain(debug)
        self.train_recommender(debug)
        self.train_conversation(debug)