import argparse
import json
import os
import random

import gin
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.optim import Adam

from src.agents import mode
from src.game import Game, CircleL1, CircleL2, CosineLoss
import src.lola


def _add_dicts(a, b):
    result = dict(a)
    for k, v in b.items():
        result[k] = result.get(k, 0) + v
    return result


def _div_dict(d, n):
    result = dict(d)
    for k in result:
        result[k] /= n
    return result


@gin.configurable
def train(Sender, Recver, vocab_size, device,
          num_epochs, num_batches, num_rounds, batch_size,
          test_batch_size=100, grounded=False, savedir=None,
          loaddir=None, random_seed=None, Loss=None):

    if random_seed is not None:
        random.seed(random_seed)
        torch.manual_seed(random_seed)
        if device.type == 'cuda':
            torch.cuda.manual_seed(random_seed)

    game = Game(num_batches=num_batches,
                num_rounds=num_rounds,
                batch_size=batch_size,
                device=device)
    test_game = Game(num_batches=1,
                     num_rounds=num_rounds,
                     batch_size=test_batch_size,
                     device=device,
                     training=False)

    if Loss is None:
        loss_fn = CircleL1(game.num_points)
    else:
        loss_fn = Loss(game.num_points)

    sender = Sender(input_size=1,
                    output_size=vocab_size,
                    mode=mode.SENDER).to(device)
    recver = Recver(input_size=vocab_size,
                    output_size=1,
                    mode=mode.RECVER).to(device)

    send_opt = Adam(sender.parameters(), lr=sender.lr)
    recv_opt = Adam(recver.parameters(), lr=recver.lr)

    # Saving
    if savedir is not None:
        os.makedirs(savedir, exist_ok=True)

        with open(f'{savedir}/config.gin', 'w') as f:
            f.write(gin.operative_config_str())

        logfile = open(f'{savedir}/logs.json', 'w')
        logfile.write('[ \n')
    else:
        logfile = None

    # Loading
    if loaddir is not None:
        loaddir = os.path.join('results', loaddir)
        if os.path.exists(f'{loaddir}/models.save'):
            model_save = torch.load(f'{loaddir}/models.save')
            sender.load_state_dict(model_save['sender'])
            recver.load_state_dict(model_save['recver'])

    test_l1_errors = []

    for epoch in range(num_epochs):
        epoch_send_logs = {}
        epoch_recv_logs = {}

        # Training
        sender.train()
        recver.train()
        for b, batch in enumerate(game):
            send_round_target, recv_round_target = batch

            sender_error_list = []
            recver_error_list = []
            sender_logprob_list = []
            recver_logprob_list = []
            sender_entropy_list = []
            recver_entropy_list = []
            message_list = []
            action_list = []

            prev_send_target = torch.zeros(batch_size, 1)
            prev_message = torch.zeros(batch_size).long()
            prev_action = torch.zeros(batch_size, 1)
            prev_sender_error = torch.zeros(batch_size, 1)
            prev_recver_error = torch.zeros(batch_size, 1)

            for r in range(num_rounds):
                first_round = torch.ones(batch_size).long() if r == 0 else torch.zeros(batch_size).long()
                send_target = send_round_target[r]
                recv_target = recv_round_target[r]

                message, send_logprob, send_entropy = sender(send_target,
                                                             prev_send_target,
                                                             prev_message,
                                                             prev_sender_error,
                                                             first_round)
                action, recv_logprob, recv_entropy = recver(message,
                                                            prev_message,
                                                            prev_action,
                                                            prev_recver_error,
                                                            first_round)
                if grounded:
                    action = message + action

                message_list.append(message)
                action_list.append(action)
                sender_logprob_list.append(send_logprob)
                recver_logprob_list.append(recv_logprob)
                sender_entropy_list.append(send_entropy)
                recver_entropy_list.append(recv_entropy)
                sender_error_list.append(loss_fn(action, send_target))
                recver_error_list.append(loss_fn(action, recv_target))

                prev_send_target = send_target
                prev_message = message.clone().detach()
                prev_action = action.clone().detach()
                prev_sender_error = sender_error_list[-1].clone().detach()
                prev_recver_error = recver_error_list[-1].clone().detach()


            messages = torch.stack(message_list, dim=0)
            actions = torch.stack(action_list, dim=0)
            sender_logprobs = torch.stack(sender_logprob_list, dim=0)
            recver_logprobs = torch.stack(recver_logprob_list, dim=0)
            sender_entropy = torch.stack(sender_entropy_list, dim=0)
            recver_entropy = torch.stack(recver_entropy_list, dim=0)
            sender_errors = torch.stack(sender_error_list, dim=0).squeeze(2)
            recver_errors = torch.stack(recver_error_list, dim=0).squeeze(2)

            if sender.lola is True:
                send_loss, send_logs = sender.loss(sender_errors, messages, sender_logprobs, sender_entropy, batch, recver, loss_fn)
            else:
                send_loss, send_logs = sender.loss(sender_errors, sender_logprobs, sender_entropy)

            if recver.lola is True:
                recv_loss, recv_logs = recver.loss(recver_errors, recver_logprobs, recver_entropy, batch, sender, loss_fn)
            else:
                recv_loss, recv_logs = recver.loss(recver_errors, recver_logprobs, recver_entropy)

            # sender must be updated before recver if using retain_graph
            send_opt.zero_grad()
            send_loss.backward(retain_graph=sender.retain_graph)
            send_opt.step()

            recv_opt.zero_grad()
            recv_loss.backward()
            recv_opt.step()

            epoch_send_logs = _add_dicts(epoch_send_logs, send_logs)
            epoch_recv_logs = _add_dicts(epoch_recv_logs, recv_logs)

        epoch_send_logs = _div_dict(epoch_send_logs, game.num_batches)
        epoch_recv_logs = _div_dict(epoch_recv_logs, game.num_batches)

        # Testing
        l1_loss_fn = CircleL1(game.num_points)
        sender.eval()
        recver.eval()
        epoch_send_test_error = 0
        epoch_recv_test_error = 0
        epoch_send_test_l1_error = 0
        epoch_recv_test_l1_error = 0

        for b, batch in enumerate(test_game):
            send_round_target, recv_round_target = batch
            send_test_error_list = []
            recv_test_error_list = []
            send_test_l1_error_list = []
            recv_test_l1_error_list = []

            prev_send_target = torch.zeros(test_batch_size, 1)
            prev_message = torch.zeros(test_batch_size).long()
            prev_action = torch.zeros(test_batch_size, 1)
            prev_sender_error = torch.zeros(test_batch_size, 1)
            prev_recver_error = torch.zeros(test_batch_size, 1)

            for r in range(num_rounds):
                first_round = torch.ones(test_batch_size).long() if r == 0 else torch.zeros(test_batch_size).long()
                send_target = send_round_target[r]
                recv_target = recv_round_target[r]

                message, send_logprob, send_entropy = sender(send_target,
                                                             prev_send_target,
                                                             prev_message,
                                                             prev_sender_error,
                                                             first_round)
                action, recv_logprob, recv_entropy = recver(message,
                                                            prev_message,
                                                            prev_action,
                                                            prev_recver_error,
                                                            first_round)

                if grounded:
                    action = message + action

                send_test_error_list.append(loss_fn(action, send_target))
                recv_test_error_list.append(loss_fn(action, recv_target))
                send_test_l1_error_list.append(l1_loss_fn(action, send_target))
                recv_test_l1_error_list.append(l1_loss_fn(action, recv_target))

                prev_send_target = send_target
                prev_message = message.clone().detach()
                prev_action = action.clone().detach()
                prev_sender_error = send_test_error_list[-1].clone().detach()
                prev_recver_error = recv_test_error_list[-1].clone().detach()

            epoch_send_test_error += torch.stack(send_test_error_list).mean().item()
            epoch_recv_test_error += torch.stack(recv_test_error_list).mean().item()
            epoch_send_test_l1_error += torch.stack(send_test_l1_error_list).mean().item()
            epoch_recv_test_l1_error += torch.stack(recv_test_l1_error_list).mean().item()

        epoch_send_logs['test_error'] = epoch_send_test_error / test_game.num_batches
        epoch_recv_logs['test_error'] = epoch_recv_test_error / test_game.num_batches
        epoch_send_logs['test_l1_error'] = epoch_send_test_l1_error / test_game.num_batches
        epoch_recv_logs['test_l1_error'] = epoch_recv_test_l1_error / test_game.num_batches

        print(f'EPOCH {epoch}')
        print(f'ERROR {epoch_send_logs["error"]:2.2f} {epoch_recv_logs["error"]:2.2f}')
        print(f'LOSS  {epoch_send_logs["loss"]:2.2f} {epoch_recv_logs["loss"]:2.2f}')
        print(f'TEST  {epoch_send_logs["test_error"]:2.2f} {epoch_recv_logs["test_error"]:2.2f}')
        print(f'L1    {epoch_send_logs["test_l1_error"]:2.2f} {epoch_recv_logs["test_l1_error"]:2.2f}\n')

        test_l1_errors.append(epoch_send_logs['test_l1_error'] + epoch_recv_logs['test_l1_error'])

        if logfile:
            if epoch > 0:
                logfile.write(',\n')
            dump = {'epoch': epoch,
                    'sender': epoch_send_logs,
                    'recver': epoch_recv_logs}
            json.dump(dump, logfile, indent=2)


    if logfile:
        logfile.write('\n]')
        logfile.close()
        torch.save({'sender': sender.state_dict(),
                    'recver': recver.state_dict(),
                    }, f'{savedir}/models.save')

    last_errors_avg = sum(test_l1_errors[-10:]) / 10
    print(f'Game Over: {last_errors_avg:2.2f}')

    return last_errors_avg


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gin_file', '-f', nargs='+')
    parser.add_argument('--gin_param', '-p', nargs='+')
    args = parser.parse_args()

    # change device to torch.device
    gin.config.register_finalize_hook(
        lambda config: config[('', '__main__.train')].update({'device': torch.device(config[('', '__main__.train')]['device'])}))
    gin.parse_config_files_and_bindings(args.gin_file, args.gin_param)

    print(gin.operative_config_str())
    train()

    # gin.clear_config()
    # gin.config._REGISTRY._selector_map.pop('__main__.train')
