"""Hyper-parameter search by Bayesian optimization
Usage:
  hyperparam_search.py --dataset=<str> [--continue_log=<filename>]
  hyperparam_search.py (-h | --help)
  hyperparam_search.py --version
Options:
  -h --help                       Show this screen.
  --version                       Show version.
  --train_path=<str>              Name of file the dataset, either wiki, 20news, or wos  (processed by sentencepeice)
  [--continue_log=<filename>]     Name of the json log file that we want the Bayesian optimization continues
"""


import os
import re
# import torch
import pathlib
import joblib
import multiprocessing
import sentencepiece as spm
import numpy as np
import random
from scipy.sparse import lil_matrix
from scipy.stats import truncnorm
from datetime import datetime
from docopt import docopt
from sklearn.feature_extraction.text import CountVectorizer

from classify import train_model
from utils import read_vocab, read_n_encode_dataset, hash_dataset_
# from evolve_on_budget import Fly

from bayes_opt import BayesianOptimization
from bayes_opt.logger import JSONLogger
from bayes_opt.event import Events
from bayes_opt.util import load_logs


# # util
# def near_shuffle(pn_size):
#     def _truncated_normal(mean, sd, low, high):
#         return truncnorm((low - mean) / sd, (high - mean) / sd, loc=mean, scale=sd)
#
#     ignore_idx = 50
#     idx_list = list(range(ignore_idx, pn_size))
#     new_idx_list = [int(_truncated_normal(mean=i, sd=10, low=ignore_idx, high=pn_size).rvs(size=1))
#                     for i in range(ignore_idx, pn_size)]


class Fly:
    def __init__(self, pn_size, kc_size=None, wta=None, num_proj=None, num_nonzero=None, init_type=3):
        self.kc_size = kc_size
        self.wta = num_nonzero / kc_size * 100
        self.projections = self.create_projections(num_proj, pn_size, init_type=init_type)
        self.val_scores = [0, 0, 0]
        self.kc_score = 1 / np.log10(int(self.kc_size * self.wta / 100))
        self.is_evaluated = False

    def create_projections(self, proj_size, pn_size, init_type):
        weight_mat = np.zeros((self.kc_size, pn_size))

        # uniformly random init
        if init_type == 0:
            for i in range(self.kc_size):
                for j in np.random.randint(pn_size, size=proj_size):
                    weight_mat[i, j] = 1

        # uniformly random init, cover all vocab
        elif init_type == 1:
            idx = list(range(pn_size))
            c = 0
            while c < self.kc_size:
                random.shuffle(idx)
                for i in range(0, len(idx), proj_size):
                    p = idx[i:i + proj_size]
                    for j in p:
                        weight_mat[c][j] = 1
                    c += 1
                    if c >= self.kc_size:
                        break

        # frequent words have more chance to form projections
        # in the vocab list, more frequent words appears on top
        # create a probability distribution on idx, then sample from the distribution
        elif init_type == 2:
            pn_idx = np.arange(50, pn_size)  # ignore punctuations, stop words, etc.
            prob_dist = (1 / pn_idx) / np.sum(1 / pn_idx)
            proj_idx = [([i] * proj_size, np.random.choice(pn_idx, proj_size, p=prob_dist))
                        for i in range(self.kc_size)]
            rows, cols = zip(*proj_idx)
            weight_mat[rows, cols] = 1

        # frequent words have more chance to form projections
        # cover all the vocab
        else:
            idx = list(range(pn_size))
            c = 0
            while c < self.kc_size:
                idx_1, idx_2 = idx[:int(0.1*len(idx))], idx[int(0.1*len(idx)):]
                idx_shuffle = random.sample(idx_1, len(idx_1)) + random.sample(idx_2, len(idx_2))
                for i in range(0, len(idx_shuffle), proj_size):
                    p = idx_shuffle[i:i + proj_size]
                    for j in p:
                        weight_mat[c][j] = 1
                    c += 1
                    if c >= self.kc_size:
                        break

        return lil_matrix(weight_mat)

    def get_vocab_coverage(self):
        ps = self.projections.toarray()
        zs = np.where(~ps.any(axis=1))[0]
        return 1 - (zs.shape[0] / self.kc_size)


def fruitfly_pipeline(top_word, KC_size, proj_size, num_nonzero,
                      C, num_iter, num_trial):
    def _hash_n_train(fly):
        hash_train = hash_dataset_(dataset_mat=train_set, weight_mat=fly.projections,
                                   percent_hash=fly.wta, top_words=top_word)
        hash_val = hash_dataset_(dataset_mat=val_set, weight_mat=fly.projections,
                                 percent_hash=fly.wta, top_words=top_word)
        val_score, model = train_model(m_train=hash_train, classes_train=train_label,
                                       m_val=hash_val, classes_val=val_label,
                                       C=C, num_iter=num_iter)
        return val_score, model

    print('creating projections')
    fly_list = [Fly(pn_size=PN_SIZE, kc_size=KC_size,
                    wta=None, num_proj=proj_size, num_nonzero=num_nonzero) for _ in range(num_trial)]

    print('training')
    score_list, model_list = [], []
    score_model_list = joblib.Parallel(n_jobs=max_thread, prefer="threads")(
        joblib.delayed(_hash_n_train)(fly) for fly in fly_list)
    score_list += [i[0] for i in score_model_list]
    model_list += [i[1] for i in score_model_list]

    # select the max performance
    max_idx = np.argmax(score_list)
    save_name = 'kc' + str(KC_size) + '_proj' + str(proj_size) +\
                '_top' + str(top_word) + '_nonzero' + str(num_nonzero) + '_C' + str(C) + \
                '_iter' + str(num_iter) + '_score' + str(score_list[max_idx])[2:]  # remove 0.
    global max_val_score
    if score_list[max_idx] > max_val_score:
        max_val_score = score_list[max_idx]
        for f in pathlib.Path(f'./models/classification/{dataset_name}_{now}').glob('*.sav'):
            f.unlink()
        joblib.dump(model_list[max_idx], f'./models/classification/{dataset_name}_{now}/{save_name}.sav')

    # average the validation acc
    avg_score = np.mean(score_list)
    std_score = np.std(score_list)
    print('average score:', avg_score)

    # write the std
    with open(f'./log/logs_{dataset_name}.tsv', 'a') as f:
        f.writelines('\t'.join(str(i) for i in [KC_size, proj_size, top_word,
                                                num_nonzero, C, num_iter, avg_score, std_score]))
        f.writelines('\n')

    return avg_score


def optimize_fruitfly(continue_log):
    def _classify(topword, KC_size, proj_size, C):
        topword = round(topword)
        KC_size = round(KC_size)
        proj_size = round(proj_size)
        num_iter=2000
        num_trial = 3
        num_nonzero = 300
        percent_hash = num_nonzero / KC_size * 100
        # if dataset_name == '20news':
        #     num_iter = 2000  # 50 wos wiki, 2000 20news
        print(f'--- KC_size {KC_size}, proj_size {proj_size}, '
              f'top_word {topword}, wta {percent_hash}, C {C}, num_iter {num_iter} ---')
        return fruitfly_pipeline(topword, KC_size, proj_size, num_nonzero,
                                 C, num_iter, num_trial)

    optimizer = BayesianOptimization(
        f=_classify,
        pbounds={"topword": (400, 400.1), "KC_size": (300, 15000),
                 "proj_size": (2, 20), "C": (1, 1.1)
                 # "percent_hash": (5, 20),
                 # 'C':(C), 'num_iter':(num_iter)
                 },
        # random_state=1234,
        verbose=2
    )

    if continue_log:
        load_logs(optimizer, logs=[continue_log])
        print("Optimizer is now aware of {} points.".format(len(optimizer.space)))
    tmp_log_path = f'./log/logs_{dataset_name}_{now}.json'
    logger = JSONLogger(path=tmp_log_path)
    optimizer.subscribe(Events.OPTIMIZATION_STEP, logger)

    optimizer.maximize(n_iter=400, init_points=100)
    print("Final result:", optimizer.max)
    with open(main_log_path, 'a') as f_main:
        with open(tmp_log_path) as f_tmp:
            tmp_log = f_tmp.read()
            f_main.write(tmp_log)


if __name__ == '__main__':
    args = docopt(__doc__, version='Hyper-parameter search by Bayesian optimization, ver 0.1')
    dataset = args["--dataset"]
    continue_log = args["--continue_log"]
    # num_iter = args["--num_iter"]
    # C = args["--C"]

    if dataset == "wiki":
        train_path="../datasets/wikipedia/wikipedia-train.sp"
        spm_model = "../spm/spm.wikipedia.model"
        spm_vocab = "../spm/spm.wikipedia.vocab"
    if dataset == "20news":
        train_path="../datasets/20news-bydate/20news-bydate-train.sp"
        spm_model = "../spm/spm.20news.model"
        spm_vocab = "../spm/spm.20news.vocab"
    if dataset == "wos":
        train_path="../datasets/wos/wos11967-train.sp"
        spm_model = "../spm/spm.wos.model"
        spm_vocab = "../spm/spm.wos.vocab"

    dataset_name = train_path.split('/')[2].split('-')[0]
    print('Dataset name:', dataset_name)

    pathlib.Path('./log').mkdir(parents=True, exist_ok=True)
    main_log_path = f'./log/logs_{dataset_name}.json'
    pathlib.Path(main_log_path).touch(exist_ok=True)
    pathlib.Path(f'./models/projection/{dataset_name}').mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pathlib.Path(f'./models/classification/{dataset_name}_{now}').mkdir(parents=True, exist_ok=True)

    # global variables
    sp = spm.SentencePieceProcessor()
    sp.load(spm_model)
    vocab, reverse_vocab, logprobs = read_vocab(spm_vocab)
    PN_SIZE = len(vocab)
    vectorizer = CountVectorizer(vocabulary=vocab, lowercase=True, token_pattern='[^ ]+')
    print('reading dataset')
    train_set, train_label = read_n_encode_dataset(train_path, vectorizer, logprobs)
    val_set, val_label = read_n_encode_dataset(train_path.replace('train', 'val'), vectorizer, logprobs)
    max_val_score = -1
    max_thread = int(multiprocessing.cpu_count() * 0.2)

    # search
    optimize_fruitfly(continue_log)


