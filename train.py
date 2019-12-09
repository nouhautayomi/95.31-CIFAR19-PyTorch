import pdb
import argparse
import numpy as np
from tqdm import tqdm

import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
from torch.optim.lr_scheduler import MultiStepLR

from torchvision.utils import make_grid
from torchvision import datasets, transforms

from misc import CSVLogger
from cutout import Cutout

from resnet import ResNet18
from wide_resnet import WideResNet

from lookahead_pytorch import Lookahead
from radam import RAdam

model_options = ['resnet18']
dataset_options = ['cifar10', 'cifar100']
optimizer_options = ['SGD','AdamW','RAdam']

test_type = True
#test_type is True when you can test acc, is False when you can watch convergence_rate
if test_type==True:
    epo = 200
else:
    epo = 60

parser = argparse.ArgumentParser(description='CNN')
parser.add_argument('--dataset', '-d', default='cifar10',choices=dataset_options)
parser.add_argument('--model', '-a', default='resnet18',choices=model_options)
parser.add_argument('--optimizer', '-b', default='SGD',choices=optimizer_options)
parser.add_argument('--batch_size', type=int, default=128, help='input batch size for training (default: 128)')
parser.add_argument('--epochs', type=int, default=epo, help='number of epochs to train (default: 200)')
parser.add_argument('--learning_rate', type=float, default=0.1,help='learning rate')
parser.add_argument('--data_augmentation', action='store_true', default=test_type, help='augment data by flipping and cropping')
parser.add_argument('--cutout', action='store_true', default=False,help='apply cutout')
parser.add_argument('--n_holes', type=int, default=1, help='number of holes to cut out from image')
parser.add_argument('--length', type=int, default=16, help='length of the holes')
parser.add_argument('--no-cuda', action='store_true', default=False, help='enables CUDA training')
parser.add_argument('--seed', type=int, default=1,help='random seed (default: 1)')
parser.add_argument('--lookahead', action='store_true', default=False)
parser.add_argument('--la_steps', type=int, default=5)
parser.add_argument('--la_alpha', type=float, default=0.5)
parser.add_argument('--AMSGrad', action='store_true', default=False)

args = parser.parse_args(args=[])
args.cuda = not args.no_cuda and torch.cuda.is_available()
cudnn.benchmark = True  # Should make training should go faster for large models

torch.manual_seed(args.seed)
if args.cuda:
    torch.cuda.manual_seed(args.seed)

test_id = args.dataset + '_' + args.model

print(args)

# Image Preprocessing
normalize = transforms.Normalize(mean=[x / 255.0 for x in [125.3, 123.0, 113.9]],
                                     std=[x / 255.0 for x in [63.0, 62.1, 66.7]])

train_transform = transforms.Compose([])
if args.data_augmentation:
    train_transform.transforms.append(transforms.RandomCrop(32, padding=4))
    train_transform.transforms.append(transforms.RandomHorizontalFlip())
train_transform.transforms.append(transforms.ToTensor())
train_transform.transforms.append(normalize)
if args.cutout:
    train_transform.transforms.append(Cutout(n_holes=args.n_holes, length=args.length))


test_transform = transforms.Compose([
    transforms.ToTensor(),
    normalize])

if args.dataset == 'cifar10':
    num_classes = 10
    train_dataset = datasets.CIFAR10(root='data/',
                                     train=True,
                                     transform=train_transform,
                                     download=True)

    test_dataset = datasets.CIFAR10(root='data/',
                                    train=False,
                                    transform=test_transform,
                                    download=True)
elif args.dataset == 'cifar100':
    num_classes = 100
    train_dataset = datasets.CIFAR100(root='data/',
                                      train=True,
                                      transform=train_transform,
                                      download=True)

    test_dataset = datasets.CIFAR100(root='data/',
                                     train=False,
                                     transform=test_transform,
                                     download=True)
    # Combine both training splits (https://arxiv.org/pdf/1605.07146.pdf)
    data = np.concatenate([train_dataset.data, extra_dataset.data], axis=0)
    labels = np.concatenate([train_dataset.labels, extra_dataset.labels], axis=0)
    train_dataset.data = data
    train_dataset.labels = labels

    test_dataset = datasets.SVHN(root='data/',
                                 split='test',
                                 transform=test_transform,
                                 download=True)

# Data Loader (Input Pipeline)
train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                           batch_size=args.batch_size,
                                           shuffle=True,
                                           pin_memory=True,
                                           num_workers=2)

test_loader = torch.utils.data.DataLoader(dataset=test_dataset,
                                          batch_size=args.batch_size,
                                          shuffle=False,
                                          pin_memory=True,
                                          num_workers=2)

cnn = ResNet18(num_classes=num_classes)
cnn = cnn.cuda()
criterion = nn.CrossEntropyLoss().cuda()

if args.optimizer == 'SGD':
    cnn_optimizer = torch.optim.SGD(cnn.parameters(), lr=args.learning_rate,momentum=0.9, nesterov=True, weight_decay=5e-4)
elif args.optimizer == 'RAdam':
    cnn_optimizer = RAdam(cnn.parameters(), lr=1e-3, betas=(0.9, 0.999), eps=1e-8, weight_decay=0.1, degenerated_to_sgd=True, AMSGrad=args.AMSGrad)
elif args.optimizer == 'AdamW':
    cnn_optimizer = torch.optim.AdamW(cnn.parameters(), lr=1e-3, betas=(0.9, 0.999), eps=1e-8,weight_decay=0.1, amsgrad=args.AMSGrad)

if args.lookahead:
    cnn_optimizer = Lookahead(cnn_optimizer, la_steps=args.la_steps, la_alpha=args.la_alpha)

scheduler = MultiStepLR(cnn_optimizer, milestones=[60, 120, 160], gamma=0.2)

filename = test_id + '.csv'
csv_logger = CSVLogger(args=args, fieldnames=['epoch', 'train_loss', 'test_acc'], filename=filename)


def test(loader):
    cnn.eval()    # Change model to 'eval' mode (BN uses moving mean/var).
    correct = 0.
    total = 0.
    for images, labels in loader:
        images = images.cuda()
        labels = labels.cuda()

        with torch.no_grad():
            pred = cnn(images)

        pred = torch.max(pred.data, 1)[1]
        total += labels.size(0)
        correct += (pred == labels).sum().item()

    val_acc = correct / total
    cnn.train()
    return val_acc


for epoch in range(args.epochs):

    xentropy_loss_avg = 0.
    correct = 0.
    total = 0.

    progress_bar = tqdm(train_loader)
    for i, (images, labels) in enumerate(progress_bar):
        progress_bar.set_description('Epoch ' + str(epoch))

        images = images.cuda()
        labels = labels.cuda()

        cnn.zero_grad()
        pred = cnn(images)

        xentropy_loss = criterion(pred, labels)
        xentropy_loss.backward()
        cnn_optimizer.step()

        xentropy_loss_avg += xentropy_loss.item()

        # Calculate running average of accuracy
        pred = torch.max(pred.data, 1)[1]
        total += labels.size(0)
        correct += (pred == labels.data).sum().item()
        accuracy = correct / total
        xentropy = xentropy_loss_avg / (i + 1)

        progress_bar.set_postfix(
            xentropy='%.4f' % (xentropy),
            acc='%.4f' % accuracy)

    if args.lookahead:
        cnn_optimizer._backup_and_load_cache()
        test_acc = test(test_loader)
        tqdm.write('test_acc: %.4f' % (test_acc))
        cnn_optimizer._clear_and_load_backup()
    else:
        test_acc = test(test_loader)
        tqdm.write('test_acc: %.4f' % (test_acc))

    scheduler.step(epoch)

    row = {'epoch': str(epoch), 'train_loss': str(xentropy), 'test_acc': str(test_acc)}
    csv_logger.writerow(row)

torch.save(cnn.state_dict(), test_id + '.pt')
csv_logger.close()
