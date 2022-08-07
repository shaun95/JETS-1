import argparse
import os

import torch
import yaml
import torch.nn as nn
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from utils.model import get_model, get_param_num
from utils.tools import to_device, log, clip_grad_value_
from model import JETSLoss
from data_utils import AudioTextDataset, AudioTextCollate, DataLoader
from evaluate import evaluate
from model.hifigan.env import AttrDict
import json
import random
random.seed(1234)


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

torch.autograd.set_detect_anomaly(True)
def main(args, configs):
    print("Prepare training ...")

    preprocess_config, model_configs, train_config = configs
    hop_size = preprocess_config["preprocessing"]["stft"]["hop_length"]

    dataset = AudioTextDataset(
        preprocess_config['path']['training_files'], preprocess_config)
    
    batch_size = train_config["optimizer"]["batch_size"]
    collate_fn = AudioTextCollate()
    loader = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=True,
        collate_fn=collate_fn, 
        num_workers=8, 
        pin_memory=True, 
        drop_last=True
    )

    # Prepare model
    (model, discriminator, 
     model_optimizer, discriminator_optimizer, 
     scheduler_model, scheduler_discriminator, 
     epoch) = get_model(
        args, configs, device, train=True)
    
    model = nn.DataParallel(model)
    discriminator = nn.DataParallel(discriminator)
    model_num_param = get_param_num(model)
    discriminator_num_param = get_param_num(discriminator)
    Loss = JETSLoss(preprocess_config, model_configs, train_config).to(device)
    print("Number of JETS Parameters:", model_num_param)
    print("Number of Discriminator Parameters:", discriminator_num_param)

    # Init logger
    for p in train_config["path"].values():
        os.makedirs(p, exist_ok=True)
    train_log_path = os.path.join(train_config["path"]["log_path"], "train")
    val_log_path = os.path.join(train_config["path"]["log_path"], "val")
    os.makedirs(train_log_path, exist_ok=True)
    os.makedirs(val_log_path, exist_ok=True)
    train_logger = SummaryWriter(train_log_path)
    val_logger = SummaryWriter(val_log_path)

    # Training
    step = args.restore_step + 1
    grad_acc_step = train_config["optimizer"]["grad_acc_step"]
    total_step = train_config["step"]["total_step"]
    log_step = train_config["step"]["log_step"]
    save_step = train_config["step"]["save_step"]
    val_step = train_config["step"]["val_step"]

    outer_bar = tqdm(total=total_step, desc="Training", position=0)
    outer_bar.n = args.restore_step
    outer_bar.update()
    
    while True:
        inner_bar = tqdm(total=len(loader), desc="Epoch {}".format(epoch), position=1)
        for batch in loader:
            batch = to_device(batch, device)
            
            output = model(*(batch[:-1]), step=step, gen=False)

            # wav_predictions, wav_targets, indices
            wav_predictions, indices = output[0], output[7]
            wav_targets = batch[-1].unsqueeze(1)[...,indices[0]*hop_size:indices[1]*hop_size]

            # Discriminator
            y_d_hat_r, y_d_hat_g, _, _ = discriminator(wav_targets, wav_predictions.detach())

            loss_disc, losses_disc = Loss.disc_loss_fn(
                disc_real_outputs=y_d_hat_r, disc_generated_outputs=y_d_hat_g)

            # Discriminator Backward
            discriminator_optimizer.zero_grad()
            loss_disc.backward()
            grad_norm_discriminator = clip_grad_value_(discriminator.parameters(), None)
            discriminator_optimizer.step()
            
            # Generator
            y_d_hat_r, y_d_hat_g, fmap_r, fmap_g = discriminator(wav_targets, wav_predictions)

            loss_model, losses_model = Loss.gen_loss_fn(
                inputs=batch, 
                predictions=output, 
                step=step, 
                disc_outputs=y_d_hat_g, 
                fmap_r=fmap_r, 
                fmap_g=fmap_g)
                    
            # Generator Backward
            model_optimizer.zero_grad()
            loss_model.backward()
            grad_norm_model = clip_grad_value_(model.parameters(), None)
            model_optimizer.step()
            
            if step % log_step == 0:
                lr = model_optimizer.param_groups[0]['lr']
                message1 = "Step {}/{}, ".format(step, total_step)
                message2 = " ".join(
                    [str(round(l.item(), 4)) for l in losses_disc.values()] + 
                    [str(round(l.item(), 4)) for l in losses_model.values()] + 
                    [str(round(grad_norm_model, 4)), str(round(grad_norm_discriminator, 4)), str(lr)]
                ).strip()

                with open(os.path.join(train_log_path, "log.txt"), "a") as f:
                    f.write(message1 + message2 + "\n")

                outer_bar.write(message1 + message2)
                
                scalars = {}
                scalars.update(losses_disc)
                scalars.update(losses_model)
                scalars.update(
                    {
                        "learning_rate": lr, 
                        "grad_norm_discriminator": grad_norm_discriminator, 
                        "grad_norm_model": grad_norm_model
                    }
                )
                log(writer=train_logger,
                    global_step=step, 
                    scalars=scalars)

            if step % val_step == 0:
                model.eval()
                discriminator.eval()
                message = evaluate([model, discriminator], step, configs, device, val_logger)
                with open(os.path.join(val_log_path, "log.txt"), "a") as f:
                    f.write(message + "\n")
                outer_bar.write(message)

                model.train()
                discriminator.train()

            if step % save_step == 0:
                torch.save(
                    {
                        "model": model.module.state_dict(),
                        "discriminator": model.module.state_dict(),
                        "model_optimizer": model_optimizer.state_dict(),
                        "discriminator_optimizer": discriminator_optimizer.state_dict(),
                        "iteration": epoch, 
                    },
                    os.path.join(
                        train_config["path"]["ckpt_path"],
                        "{}.pth.tar".format(step),
                    ),
                )

            if step == total_step:
                quit()
            step += 1
            outer_bar.update(1)

        epoch += 1
        scheduler_model.step()
        scheduler_discriminator.step()
        inner_bar.update(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore_step", type=int, default=0)
    parser.add_argument(
        "-p",
        "--preprocess_config",
        type=str,
        required=True,
        help="path to preprocess.yaml",
    )
    parser.add_argument(
        "-s", "--synthesizer_config", type=str, required=True, help="path to model.yaml"
    )
    parser.add_argument(
        "-g", "--generator_config", type=str, required=True, help="path to config_v1.json"
    )
    parser.add_argument(
        "-t", "--train_config", type=str, required=True, help="path to train.yaml"
    )
    args = parser.parse_args()

    # Read Config
    preprocess_config = yaml.load(
        open(args.preprocess_config, "r"), Loader=yaml.FullLoader
    )
    synthesizer_config = yaml.load(open(args.synthesizer_config, "r"), Loader=yaml.FullLoader)
    train_config = yaml.load(open(args.train_config, "r"), Loader=yaml.FullLoader)
    with open(args.generator_config, "r", encoding='utf8') as j:
        generator_config = json.loads(j.read())
        generator_config = AttrDict(generator_config)

    configs = (preprocess_config, [synthesizer_config, generator_config], train_config)

    main(args, configs)
