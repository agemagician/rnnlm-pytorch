import argparse
import time
import math
import os
import torch
import torch.nn as nn

import pickle

import data
import models

import horovod.torch as hvd
import torch.backends.cudnn as cudnn

def options():
    parser = argparse.ArgumentParser(description='PyTorch RNN/LSTM Language Model')
    parser.add_argument('--data', type=str, default='./dataset',
                    help='location of the data corpus')
    parser.add_argument('--glove', type=str, default='',
                    help='path to the glove embedding')
    parser.add_argument('--rnn_type', type=str, default='ResLSTM',
                    help='type of recurrent net (RNN_TANH, RNN_RELU, LSTM, GRU, ResRNN_TANH, ResRNN_RELU, ResLSTM, ResGRU)')
    parser.add_argument('--direction', type=str, default='left2right',
                    help='type of language model direction (left2right, right2left, both)')
    parser.add_argument('--wo_tok', action='store_true',
                    help='without token embeddings')
    parser.add_argument('--wo_char', action='store_true',
                    help='without character embeddings')
    parser.add_argument('--tok_emb', type=int, default=200,
                    help='The dimension size of word embeddings')
    parser.add_argument('--char_emb', type=int, default=50,
                    help='The dimension size of character embeddings')
    parser.add_argument('--char_kmin', type=int, default=1,
                    help='minimum size of the kernel in the character encoder')
    parser.add_argument('--char_kmax', type=int, default=5,
                    help='maximum size of the kernel in the character encoder')
    parser.add_argument('--tok_hid', type=int, default=250,
                    help='number of hidden units of the token level rnn layer')
    parser.add_argument('--char_hid', type=int, default=50,
                    help='number of hidden units of the character level rnn layer')
    parser.add_argument('--nlayers', type=int, default=4,
                    help='number of layers')
    parser.add_argument('--optim_type', type=str, default='SGD',
                    help='type of the optimizer')
    parser.add_argument('--lr', type=float, default=20,
                    help='initial learning rate')
    parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
    parser.add_argument('--epochs', type=int, default=40,
                    help='upper epoch limit')
    parser.add_argument('--batch_size', type=int, default=20, metavar='N',
                    help='batch size')
    parser.add_argument('--cut_freq', type=int, default=10,
                    help='cut off tokens in a corpus less than this value')
    parser.add_argument('--max_vocab_size', type=int, default=100000,
                    help='cut off low-frequencey tokens in a corpus if the vocabulary size exceeds this value')
    parser.add_argument('--max_length', type=int, default=300,
                    help='skip sentences more than this value')
    parser.add_argument('--dropout', type=float, default=0.2,
                    help='dropout applied to layers (0 = no dropout)')
    parser.add_argument('--init_range', type=float, default=0.1,
                    help='initialization range of the weights')
    parser.add_argument('--tied', action='store_true',
                    help='tie the word embedding and softmax weights')
    parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
    parser.add_argument('--cuda', action='store_true',
                    help='use CUDA')
    parser.add_argument('--lms', action='store_true',
                    help='use CUDA')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                    help='report interval')
    parser.add_argument('--pretrain', type=str, default='',
                    help='prefix to pretrained model')
    parser.add_argument('--save', type=str, default='./models/model',
                    help='prefix to save the final model')
    parser.add_argument('--dict', type=str, default='./models/dict.pkl',
                    help='path to (save/load) the dictionary')
    
    parser.add_argument('--fp16-allreduce', action='store_true', default=False,
                    help='use fp16 compression during allreduce')
    parser.add_argument('--batches-per-allreduce', type=int, default=1,
                    help='number of batches processed locally before '
                         'executing allreduce across workers; it multiplies '
                         'total batch size.')
    parser.add_argument('--use-adasum', action='store_true', default=False,
                    help='use adasum algorithm to do reduction')
    
    #parser.add_argument('--checkpoint-format', default='./checkpoint-{epoch}.pth.tar',
    #                help='checkpoint file format')

    opts = parser.parse_args()
    return opts

def evaluate(opts, device, corpus, model, criterion, epoch):
    """
    Parameters
    ----------
        opts: command line arguments
        device: device type
        corpus: Corpus
        model: Model
        criterion: loss function
        epoch: current epoch
    Return
    ------
        total_loss: float
    """
    epoch_start_time = time.time()
    # Turn on evaluation mode which disables dropout.
    model.eval()
    #total_loss = 0.0
    val_loss = Metric('val_loss')
    # Do not back propagation
    with torch.no_grad():
        for batch_id, batch in enumerate(data.data2batch(corpus.valid, corpus.dictionary, opts.batch_size, flag_shuf=True)):
            hidden = model.init_hidden(batch)
            # Cut the computation graph (Initialize)
            hidden = models.repackage_hidden(hidden)
            # LongTensor of token_ids [seq_len, batch_size]
            input = model.batch2input(batch, device)
            # target_flat: LongTensor of token_ids [seq_len*batch_size]
            target_flat = model.batch2flat(batch, device)
            # clear previous gradients
            model.zero_grad()
            # output: [seq_len, nbatch, ntoken], hidden: [nlayer, nbatch, nhid]
            output, hidden = model(input, hidden)
            # output_flat: LongTensor of token_ids [seq_len*batch_size, ntoken]
            output_flat = output.view(-1, output.shape[2])
            # target_flat: LongTensor of token_ids [seq_len*batch_size]
            #total_loss += criterion(output_flat, target_flat).item()
            val_loss.update(criterion(output_flat, target_flat))
            total_num = batch_id + 1
    #total_loss /= total_num
    total_loss = val_loss.avg.item()
    if verbose == 1:
        print('-' * 89)
        try:
            print('| end of epoch {:3d} | time: {:5.2f}s | valid loss {:5.2f} | '
                'valid ppl {:8.2f}'.format(epoch, (time.time() - epoch_start_time), total_loss, math.exp(total_loss)))
        except:
            print("Warning: math error")
        print('-' * 89)
    return total_loss

def train(opts, device, corpus, model, criterion, optimizer, lr, epoch):
    """
    Parameters
    ----------
        opts: command line arguments
        device: device type
        corpus: Corpus
        model: Model
        criterion: loss function
        optimizer: optimizer
        lr: learning rate (float)
        epoch: current epoch
    """
    # Turn on training mode which enables dropout.
    model.train()
    #total_loss = 0.
    train_loss = Metric('train_loss')
    start_time = time.time()
    for batch_id, batch in enumerate(data.data2batch(corpus.train, corpus.dictionary, opts.batch_size, flag_shuf=True)):
        # Starting each batch, we detach the hidden state from how it was previously produced.
        # If we didn't, the model would try backpropagating all the way to start of the dataset.
        # batch[0].shape[1]: nbatch, hidden: [nlayer, nbatch, nhid]
        #print(batch)
        hidden = model.init_hidden(batch)
        # Cut the computation graph (Initialize)
        hidden = models.repackage_hidden(hidden)
        # LongTensor of token_ids [seq_len, batch_size]
        input = model.batch2input(batch, device)
        # target_flat: LongTensor of token_ids [seq_len*batch_size]
        target_flat = model.batch2flat(batch, device)
        # clear previous gradients
        model.zero_grad()
        # output: [seq_len, nbatch, ntoken], hidden: [nlayer, nbatch, nhid]
        #print(input)
        output, hidden = model(input, hidden)
        # output_flat: LongTensor of token_ids [seq_len*batch_size, ntoken]
        output_flat = output.view(-1, output.shape[2])
        # Calculate the mean of all losses. 
        # loss: float
        loss = criterion(output_flat, target_flat)
        # Do back propagetion
        loss.backward()
        # Gradient clipping
        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm_(model.parameters(), opts.clip)
        # Update weights
        """
        # SGD
        for p in model.parameters():
            p.data.add_(-lr, p.grad.data)
        """
        optimizer.step()
        optimizer.zero_grad()
        #total_loss += loss.item()
        train_loss.update(loss)
        
        if batch_id % opts.log_interval == 0 and batch_id > 0:
            #cur_loss = total_loss / opts.log_interval
            cur_loss = train_loss.avg.item()
            elapsed = time.time() - start_time
            if verbose == 1:
                print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                    'loss {:5.2f} | ppl {:8.2f}'.format(
                epoch, batch_id, len(corpus.train) // opts.batch_size, lr,
                elapsed * 1000 / opts.log_interval, cur_loss, math.exp(cur_loss)))
            #total_loss = 0
            start_time = time.time()

# Horovod: average metrics from distributed training.
class Metric(object):
    def __init__(self, name):
        self.name = name
        self.sum = torch.tensor(0.)
        self.n = torch.tensor(0.)

    def update(self, val):
        self.sum += hvd.allreduce(val.detach().cpu(), name=self.name)
        self.n += 1

    @property
    def avg(self):
        return self.sum / self.n

    
def save_params(params,savePath):
    if hvd.rank() == 0:
        with open(savePath + ".params", mode='wb') as f:
            pickle.dump(params, f)
    
def save_checkpoint(model, optimizer,epoch):
    if hvd.rank() == 0:
        epoch = epoch + 1
        #filepath = args.checkpoint_format.format(epoch=epoch + 1)
        #filepath = args.checkpoint_format.format(epoch=epoch)
        filepath = opts.save + "checkpoint-" + str(epoch) + ".pth.tar"
        state = {
            'model': model.state_dict(),
            'optimizer': optimizer.state_dict(),
        }
        #print(filepath)
        try:
            torch.save(state, filepath)
        except Exception as e:
            print(e)
        
def main():

    ###############################################################################
    # Load command line options.
    ###############################################################################
    global verbose,opts
    
    opts = options()
    # Set the random seed manually for reproducibility.
    torch.manual_seed(opts.seed)
        
    hvd.init()
    
    if opts.cuda:
        # Horovod: pin GPU to local rank.
        torch.cuda.set_device(hvd.local_rank())
        #torch.cuda.manual_seed(opts.seed)
    
    cudnn.benchmark = True
    
    # Horovod: print logs on the first worker.
    verbose = 1 if hvd.rank() == 0 else 0
    
    if opts.lms == True:
        torch.cuda.set_enabled_lms(True)
        if verbose == True:
            print('LMS is enabled')
    
    # If set > 0, will resume training from a given checkpoint.
    resume_from_epoch = 0
    for try_epoch in range(opts.epochs, 0, -1):
        filepath = opts.save + "checkpoint-" + str(try_epoch) + ".pth.tar"
        if os.path.exists(filepath):
            resume_from_epoch = try_epoch
            break

    # Horovod: broadcast resume_from_epoch from rank 0 (which will have
    # checkpoints) to other ranks.
    resume_from_epoch = hvd.broadcast(torch.tensor(resume_from_epoch), root_rank=0,
                                      name='resume_from_epoch').item()

    ###############################################################################
    # Load data
    ###############################################################################

    corpus = data.Corpus(opts)
    if opts.pretrain == "":
        corpus.make_dict(opts.data)
    else:
        corpus.load_dict()

    corpus.load_data(opts.data)
    with open(opts.dict, mode='wb') as f:
        pickle.dump(corpus.dictionary, f)

    ###############################################################################
    # Build a model
    ###############################################################################

    if opts.pretrain == "":
        # convert to parameters
        params = models.opts2params(opts, corpus.dictionary)
        # construct model
        model = models.RNNModel(params)
    # For fine-tuning
    else:
        # load parameters
        with open(opts.pretrain + ".params", 'rb') as f:
            params = pickle.load(f)
        # construct model
        model = models.RNNModel(params)
        # load pretraind model
        model.load_state_dict(torch.load(opts.pretrain + ".pt"))
        model.freeze_emb()

    # save parameters
    #with open(opts.save + ".params", mode='wb') as f:
    #    pickle.dump(params, f)
    save_params(params,opts.save)
    
    if torch.cuda.is_available():
        if not opts.cuda:
            print("WARNING: You have a CUDA device, so you should probably run with --cuda")
    else:
        if opts.cuda:
            print("Error: No CUDA device. Remove the option --cuda")
    device = torch.device("cuda" if opts.cuda else "cpu")
    model = model.to(device)
    
    # loss function (ignore padding id)
    criterion = nn.CrossEntropyLoss(ignore_index=corpus.dictionary.pad_id())

    ###############################################################################
    # Train the  model
    ###############################################################################

    # Loop over epochs.
    lr = opts.lr
    best_val_loss = None

    # Select an optimizer
    try:
        optimizer = getattr(torch.optim, opts.optim_type)(model.parameters(), lr=lr)
    except:
        raise ValueError( """An invalid option for `--optim_type` was supplied.""")
        
    # Horovod: (optional) compression algorithm.
    compression = hvd.Compression.fp16 if opts.fp16_allreduce else hvd.Compression.none

    # Horovod: wrap optimizer with DistributedOptimizer.
    try:
        optimizer = hvd.DistributedOptimizer(
        optimizer, named_parameters=model.named_parameters(),
        compression=compression,
        backward_passes_per_step=opts.batches_per_allreduce,
        op=hvd.Adasum if opts.use_adasum else hvd.Average)
    except:
        optimizer = hvd.DistributedOptimizer(
        optimizer, named_parameters=model.named_parameters(),
        compression=compression,
        backward_passes_per_step=opts.batches_per_allreduce)


    # Restore from a previous checkpoint, if initial_epoch is specified.
    # Horovod: restore on the first worker which will broadcast weights to other workers.
    if (resume_from_epoch > 0) and (hvd.rank() == 0) :
        filepath = opts.save + "checkpoint-" + str(resume_from_epoch) + ".pth.tar"
        #filepath = args.checkpoint_format.format(epoch=resume_from_epoch)
        checkpoint = torch.load(filepath)
        model.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
    
            
    # Horovod: broadcast parameters & optimizer state.
    hvd.broadcast_parameters(model.state_dict(), root_rank=0)
    hvd.broadcast_optimizer_state(optimizer, root_rank=0)

    # At any point you can hit Ctrl + C to break out of training early.
    try:
        for epoch in range(resume_from_epoch, opts.epochs):
            train(opts, device, corpus, model, criterion, optimizer, lr, epoch)
            val_loss = evaluate(opts, device, corpus, model, criterion, epoch)
            save_checkpoint(model,optimizer,epoch)
            # Save the model if the validation loss is the best we've seen so far.
            if not best_val_loss or val_loss < best_val_loss:
                #torch.save(model.state_dict(), opts.save + ".pt")
                save_checkpoint(model,optimizer,-1)
                best_val_loss = val_loss
            #else:
            #    # Anneal the learning rate if no improvement has been seen in the validation dataset.
            #    lr /= 4.0
            #optimizer.lr = lr
    except KeyboardInterrupt:
        print('-' * 89)
        print('Exiting from training early')

if __name__ == "__main__":
    main()
