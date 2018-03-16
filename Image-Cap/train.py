import argparse

parser = argparse.ArgumentParser(description='Image Cation')
parser.add_argument('--logdir', type=str, default='tb_logdir')
parser.add_argument('--seed', type=int, default=1111)
parser.add_argument('--unuse_cuda', action='store_true')
parser.add_argument('--path', type=str, default='data/')
parser.add_argument('--data', type=str, default='data/img_caption.pt')
parser.add_argument('--save', type=str, default='imgcapt_v2_{}.pt')
parser.add_argument('--pre_lr', type=float, default=5e-4)
parser.add_argument('--lr', type=float, default=5e-5)
parser.add_argument('--new_lr', type=float, default=5e-6)
parser.add_argument('--actor_epochs', type=int, default=1)
parser.add_argument('--epochs', type=int, default=40)
parser.add_argument('--iterations', type=int, default=2000)
parser.add_argument('--batch_size', type=int, default=32)
parser.add_argument('--dec_hsz', type=int, default=512)
parser.add_argument('--rnn_layers', type=int, default=1)
parser.add_argument('--dropout', type=float, default=.5)
parser.add_argument('--grad_clip', type=float, default=1.)

args = parser.parse_args()

import torch

torch.manual_seed(args.seed)
use_cuda = torch.cuda.is_available() and not args.unuse_cuda

if use_cuda:
    torch.cuda.manual_seed(args.seed)

# ##############################################################################
# Tensorboard
################################################################################
try:
    import tensorflow as tf
    tf_step = 0
except ImportError:
    tf = None

tf_summary_writer = tf and tf.summary.FileWriter(args.logdir)

def add_summary_value(key, value):
    global tf_step

    summary = tf.Summary(value=[tf.Summary.Value(tag=key, simple_value=value)])
    tf_summary_writer.add_summary(summary, tf_step)

# ##############################################################################
# Load data
################################################################################
from data_loader import Data_loader

data = torch.load(args.data)
args.max_len = data["max_word_len"]
args.dict = data["dict"]
args.vocab_size = data["vocab_size"]

training_data = Data_loader(
              "data/train2017/",
              data['train']['imgs'],
              data['train']['captions'],
              args.max_len,
              batch_size=args.batch_size,
              is_cuda=use_cuda)

validation_data = Data_loader(
              "data/val2017/",
              data['valid']['imgs'],
              data['valid']['captions'],
              args.max_len,
              batch_size=args.batch_size,
              is_cuda=use_cuda,
              evaluation=True)

# ##############################################################################
# Build model
# ##############################################################################
import model
from const import PAD
from optim import Optim

actor = model.Actor(args.vocab_size,
                    args.dec_hsz,
                    args.rnn_layers,
                    args.batch_size,
                    args.max_len,
                    args.dropout,
                    use_cuda)

critic = model.Critic(args.vocab_size,
                      args.dec_hsz,
                      args.rnn_layers,
                      args.batch_size,
                      args.max_len,
                      args.dropout,
                      use_cuda)

optim_pre_A = Optim(actor.get_trainable_parameters(), args.pre_lr, True, args.grad_clip)
optim_pre_C = Optim(critic.parameters(), args.pre_lr, True, args.grad_clip)

optim_A = Optim(actor.get_trainable_parameters(), args.lr, False, args.new_lr, args.grad_clip)
optim_C = Optim(critic.parameters(), args.lr, False, args.new_lr, args.grad_clip)

criterion_A = torch.nn.CrossEntropyLoss(ignore_index=PAD)
criterion_AC = model.RewardCriterion()

if use_cuda:
    actor = actor.cuda()
    critic = critic.cuda()

# ##############################################################################
# Training
# ##############################################################################
from tqdm import tqdm

from torch.autograd import Variable
import torch.nn.functional as F

from rouge import rouge_l, mask_score

def pre_train_actor():
    if tf: global tf_step
    for imgs, labels in tqdm(training_data,
            mininterval=1, desc="Pre-train Actor", leave=False):
        optim_pre_A.zero_grad()

        enc = actor.encode(imgs)
        hidden = actor.feed_enc(enc)
        target, _ = actor(hidden, labels)

        loss = criterion_A(target.view(-1, target.size(2)), labels.view(-1))

        loss.backward()
        optim_pre_A.clip_grad_norm()
        optim_pre_A.step()
        if tf is not None:
            add_summary_value("pre-train actor loss", loss.data[0])
            tf_step += 1

            if tf_step % 100 == 0:
                tf_summary_writer.flush()

def pre_train_critic():
    iterations = 0
    actor.eval()
    critic.train()
    if tf: global tf_step
    for imgs, labels in tqdm(training_data,
            mininterval=1, desc="Pre-train Critic", leave=False):
        optim_pre_C.zero_grad()

        enc = actor.encode(imgs)
        hidden_A = actor.feed_enc(enc)
        _, words_A = actor(hidden_A, labels)

        hidden_C = critic.feed_enc(enc)
        props_C, words_C = critic(words_A, hidden_C)

        reward = rouge_l(words_A[:, 1:], labels).sub(rouge_l(words_C, labels))[:, 0]
        loss = critic.td_error(reward, props_C, optim_pre_C)

        iterations += 1
        if tf is not None:
            add_summary_value("pre-train critic loss", loss[0])
            tf_step += 1

            if tf_step % 100 == 0:
                tf_summary_writer.flush()

        if iterations == args.iterations: break

def train_actor_critic():
    actor.train()
    critic.train()
    if tf: global tf_step

    for imgs, labels in tqdm(training_data,
            mininterval=1, desc="Actor-Critic Training", leave=False):
        optim_A.zero_grad()

        enc = actor.encode(imgs)
        hidden_A = actor.feed_enc(enc)
        _, words_A = actor(hidden_A, labels)

        hidden_C = critic.feed_enc(enc)
        props_C, words_C = critic(words_A, hidden_C)

        scores_A, scores_C = rouge_l(words_A[:, 1:], labels), rouge_l(words_C, labels)

        loss_c = critic.td_error(scores_A.sub(scores_C)[:, 0], props_C, optim_C)

        sample_words, sample_props = actor.speak(hidden_A)
        sample_score = rouge_l(sample_words, labels)

        loss_a, reward = criterion_AC(sample_props, sample_words, scores_C-sample_score)
        loss_a.backward()

        optim_A.clip_grad_norm()
        optim_A.step()

        if tf is not None:
            add_summary_value("train critic loss", loss_c[0])
            add_summary_value("train actor loss", loss_a.data[0])
            add_summary_value("train actor reward", reward.data[0])
            add_summary_value("train critic score", scores_C.mean())
            add_summary_value("train actor score", sample_score.mean())
            tf_step += 1

            if tf_step % 100 == 0:
                tf_summary_writer.flush()

def eval():
    actor.eval()
    eval_score = .0
    for imgs, labels in tqdm(validation_data,
            mininterval=1, desc="Actor-Critic Eval", leave=False):
        enc = actor.encode(imgs)

        hidden = actor.feed_enc(enc)
        words, _ = actor.speak(hidden)

        scores = rouge_l(words, labels)
        scores = scores.sum()

        eval_score += scores.data

    eval_score = eval_score[0]/validation_data.sents_size

    return eval_score

try:
    print("="*40 + "Pre-train Actor" + "="*40)
    actor.train()
    if tf: tf_step = 0
    for step in range(args.actor_epochs):
        pre_train_actor()
        model_state_dict = actor.state_dict()
        model_source = {
            "settings": args,
            "model": model_state_dict,
            "dict": data['dict']
        }
        torch.save(model_source, args.save.format("pret-actor_" + str(step)))

    if tf: tf_step = 0
    print("="*40 + "Pre-train Critic" + "="*40)
    pre_train_critic()

    if tf: tf_step = 0
    print("="*40 + "Actor-Critic Training" + "="*40)
    for step in range(args.epochs):
        train_actor_critic()
        eval_score = eval()
        print("-"*20 + "epoch-{}-eval | eval score: {:.4f}".format(step, eval_score) + "-"*20)

        model_state_dict = actor.state_dict()
        model_source = {
            "settings": args,
            "model": model_state_dict,
            "dict": data['dict']
        }
        torch.save(model_source, args.save.format(step))

except KeyboardInterrupt:
    print("-"*90)
    print("Exiting from training early")

