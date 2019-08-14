import argparse
import json
import os
import random

import gin
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.optim import Adam

from src.agents import mode
from src.game import Game, CircleLoss
import src.lola


@gin.configurable
def train(Sender, Recver, vocab_size, device,
          num_epochs, num_batches, batch_size,
          savedir=None, loaddir=None, random_seed=None):

    if random_seed is not None:
        random.seed(random_seed)
        torch.manual_seed(random_seed)
        if device.type == 'cuda':
            torch.cuda.manual_seed(random_seed)

    game = Game(num_batches=num_batches,
                batch_size=batch_size,
                device=device)
    loss_fn = CircleLoss(game.num_points)
    # loss_fn = nn.L1Loss(reduction="none")

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
        print(gin.operative_config_str())
        logfile = None

    # Loading
    if loaddir is not None:
        loaddir = os.path.join('results', loaddir)
        if os.path.exists(f'{loaddir}/models.save'):
            model_save = torch.load(f'{loaddir}/models.save')
            sender.load_state_dict(model_save['sender'])
            recver.load_state_dict(model_save['recver'])

    # Training
    best_epoch_rew = None

    for e, epoch in enumerate(range(num_epochs)):
        epoch_send_loss = 0
        epoch_recv_loss = 0
        epoch_send_rew = 0
        epoch_recv_rew = 0

        for b, batch in enumerate(game):
            send_target, recv_target = batch

            message, logprobs, entropy = sender(send_target)
            action = recver(message)

            raw_send_loss = loss_fn(action, send_target).mean(dim=1)
            raw_recv_loss = loss_fn(action, recv_target).mean(dim=1)

            # send_loss, send_logs = sender.loss(batch, recver)
            send_loss, send_logs = sender.loss(raw_send_loss, logprobs, entropy)
            recv_loss, recv_logs = recver.loss(raw_recv_loss)

            # sender MUST be update before recver
            send_opt.zero_grad()
            send_loss.backward()
            send_opt.step()

            recv_opt.zero_grad()
            recv_loss.backward()
            recv_opt.step()

            epoch_send_loss += send_loss
            epoch_recv_loss += recv_loss
            epoch_send_rew -= raw_send_loss.mean().item()
            epoch_recv_rew -= raw_recv_loss.mean().item()


        epoch_send_loss /= game.num_batches
        epoch_recv_loss /= game.num_batches
        epoch_send_rew /= game.num_batches
        epoch_recv_rew /= game.num_batches

        print(f'EPOCH {e}')
        print(f'REWD  {epoch_send_rew:2.2f} {epoch_recv_rew:2.2f}')
        print(f'LOSS  {epoch_send_loss:2.2f} {epoch_recv_loss:2.2f} \n')

        mean_epoch_rew = (epoch_send_rew + epoch_recv_rew) / 2
        if best_epoch_rew is None or mean_epoch_rew > best_epoch_rew:
            best_epoch_rew = mean_epoch_rew

        if logfile:
            dump = {'epoch': e,
                    'sender': send_logs,
                    'recver': recv_logs}
            json.dump(dump, logfile, indent=2)
            logfile.write(',\n')

    print(f'Game Over: {best_epoch_rew:2.2f}')

    if savedir:
        dump = {'epoch': e,
                'sender': send_logs,
                'recver': recv_logs}
        json.dump(dump, logfile, indent=2)
        logfile.write('\n]')
        logfile.close()
        torch.save({'sender': sender.state_dict(),
                    'recver': recver.state_dict(),
                    }, f'{savedir}/models.save')

    return best_epoch_rew


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gin_file', '-f', nargs='+')
    parser.add_argument('--gin_param', '-p', nargs='+')
    args = parser.parse_args()

    # change device to torch.device
    gin.config.register_finalize_hook(
        lambda config: config[('', '__main__.train')].update({'device': torch.device(config[('', '__main__.train')]['device'])}))
    gin.parse_config_files_and_bindings(args.gin_file, args.gin_param)

    train()

    # gin.clear_config()
    # gin.config._REGISTRY._selector_map.pop('__main__.train')
