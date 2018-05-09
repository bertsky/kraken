# -*- coding: utf-8 -*-
#
# Copyright 2015 Benjamin Kiessling
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing
# permissions and limitations under the License.

from __future__ import absolute_import, division, print_function
from future import standard_library
import torch

import os
import re
import time
import click
import errno
import base64
import unicodedata
import numpy as np

from PIL import Image
from lxml import html, etree
from io import BytesIO
from itertools import cycle
from bidi.algorithm import get_display

from torch.optim import Adam, SGD, RMSprop
from torch.autograd import Variable
from torch.utils.data import DataLoader

from kraken import rpred
from kraken import linegen
from kraken import pageseg
from kraken import transcrib
from kraken import binarization
from kraken.lib import models, vgsl
from kraken.train import GroundTruthDataset, compute_error
from kraken.lib.exceptions import KrakenCairoSurfaceException
from kraken.lib.exceptions import KrakenInputException

standard_library.install_aliases()

APP_NAME = 'kraken'

spinner = cycle([u'⣾', u'⣽', u'⣻', u'⢿', u'⡿', u'⣟', u'⣯', u'⣷'])


def spin(msg):
    click.echo(u'\r\033[?25l{}\t{}'.format(msg, next(spinner)), nl=False)


@click.group()
@click.version_option()
@click.option('-v', '--verbose', default=0, count=True)
def cli(verbose):
    ctx = click.get_current_context()
    ctx.meta['verbose'] = verbose


@cli.command('train')
@click.pass_context
@click.option('-p', '--pad', type=click.INT, default=16, help='Left and right '
              'padding around lines')
@click.option('-o', '--output', type=click.Path(), default='model', help='Output model file')
@click.option('-s', '--spec', default='[1,1,0,48 Lbx100]', help='VGSL spec of the network to train. CTC layer will be added automatically.')
@click.option('-i', '--load', type=click.Path(exists=True, readable=True), help='Load existing file to continue training')
@click.option('-F', '--savefreq', default=1, type=click.FLOAT, help='Model save frequency in epochs during training')
@click.option('-R', '--report', default=1, help='Report creation frequency in epochs')
@click.option('-N', '--epochs', default=1000, help='Number of epochs to train for')
@click.option('-d', '--device', default='cpu', help='Select device to use (cpu, gpu:0, gpu:1, ...)')
@click.option('--optimizer', default='SGD', type=click.Choice(['SGD', 'Adam', 'RMSprop']), help='Select optimizer')
@click.option('-r', '--lrate', default=1e-3, help='Learning rate')
@click.option('-w', '--wdecay', default=0.0, help='Adam weight decay')
@click.option('-p', '--partition', default=0.9, help='Ground truth data partition ratio between train/test set')
@click.option('-u', '--normalization', type=click.Choice(['NFD', 'NFKD', 'NFC', 'NFKC']), default=None, help='Ground truth normalization')
@click.option('-c', '--codec', default=None, type=click.File(mode='rb', lazy=True), help='Load a codec JSON definition (invalid if loading existing model)')
@click.option('-n', '--reorder/--no-reorder', default=True, help='Reordering of code points to display order')
@click.argument('ground_truth', nargs=-1, type=click.Path(exists=True, dir_okay=False))
def train(ctx, pad, output, spec, load, savefreq, report, epochs, device,
          optimizer, lrate, wdecay, partition, normalization, codec, reorder,
          ground_truth):
    """
    Trains a model from image-text pairs.
    """
    if load and codec:
        raise click.BadOptionUsage('codec', 'codec option is not supported when loading model')

    # preparse input sizes from vgsl string to seed ground truth data set
    # sizes and dimension ordering.
    spec = spec.strip()
    if spec[0] != '[' or spec[-1] != ']':
        raise click.BadOptionUsage('VGSL spec {} not bracketed'.format(spec))
    blocks = spec[1:-1].split(' ')
    m = re.match(r'(\d+),(\d+),(\d+),(\d+)', blocks[0])
    if not m:
        raise click.BadOptionUsage('Invalid input spec {}'.format(blocks[0]))
    batch, height, width, channels = [int(x) for x in m.groups()]
    # height 1, arbitrary width, and channels > 3 indicate 8bpp fixed height
    # (channels) strips of an arbitrary width image => swap height dimension into channels
    if height == 1 and width == 0 and channels > 3:
        format = (1, 0, 2)
        scale = channels
    # arbitrary (or fixed) height and width and channels 1 or 3 => needs a
    # summarizing network (or a not yet implemented scale operation) to move
    # height to the channel dimension.
    elif height > 1 and width == 0 and channels in (1, 3):
        format = (0, 1, 2)
        scale = height
    # fixed height and width image => bicubic scaling of the input image, disable padding
    elif height > 0 and width > 0  and channels in (1, 3):
        format = (0, 1, 2)
        pad = 0
        scale = (height, width)
    elif height == 0 and width == 0  and channels in (1, 3):
        format = (0, 1, 2)
        pad = 0
        scale = 0
    else:
        raise click.BadOptionUsage('Invalid input spec {} (variable height and fixed width not supported)'.format(blocks[0]))

    st_time = time.time()
    if ctx.meta['verbose'] > 0:
        click.echo(u'[{:2.4f}] Building training data from {} line images'.format(time.time() - st_time, len(ground_truth)))
    else:
        spin('Building training set')

    ground_truth = list(ground_truth)
    np.random.shuffle(ground_truth)
    tr_im = ground_truth[:int(len(ground_truth) * partition)]
    te_im = ground_truth[int(len(ground_truth) * partition):]

    gt_set = GroundTruthDataset(tr_im, normalization=normalization, reorder=reorder, scale=scale, pad=pad, format=format)
    for im in tr_im:
        if ctx.meta['verbose'] > 1:
            click.echo(u'[{:2.4f}] Adding line {} to training set'.format(time.time() - st_time, im))
        else:
            spin('Building training set')
        gt_set.add(im)
    if not ctx.meta['verbose'] > 0:
       click.secho(u'\b\u2713', fg='green', nl=False)
       click.echo('\033[?25h\n', nl=False)

    train_loader = DataLoader(gt_set, batch_size=1, shuffle=True)

    test_set = GroundTruthDataset(te_im, normalization=normalization, reorder=reorder, pad=pad, format=format)
    for im in te_im:
        if ctx.meta['verbose'] > 1:
            click.echo(u'[{:2.4f}] Adding line {} to test set'.format(time.time() - st_time, im))
        else:
            spin('Building test set')
        test_set.add(im)
    if not ctx.meta['verbose'] > 0:
       click.secho(u'\b\u2713', fg='green', nl=False)
       click.echo('\033[?25h\n', nl=False)

    if ctx.meta['verbose'] == 0:
        click.echo('')
    if ctx.meta['verbose'] > 0:
        click.echo(u'[{:2.4f}] Training set {} lines, test set {} lines, alphabet {} symbols'.format(time.time() - st_time, len(gt_set._images), len(test_set._images), len(gt_set.alphabet)))
    alpha_diff = set(gt_set.alphabet).symmetric_difference(set(test_set.alphabet))
    if alpha_diff:
        click.echo(u'[{:2.4f}] warning: alphabet mismatch {}'.format(time.time() - st_time, alpha_diff))
    if ctx.meta['verbose'] > 1:
        click.echo(u'[{:2.4f}] grapheme\tcount'.format(time.time() - st_time))
        for k, v in sorted(gt_set.alphabet.iteritems(), key=lambda x: x[1], reverse=True):
            if unicodedata.combining(k) or k.isspace():
                k = unicodedata.name(k)
            else:
                k = '\t' + k
            click.echo(u'[{:2.4f}] {}\t{}'.format(time.time() - st_time, k, v))

    if not ctx.meta['verbose']:
        click.secho(u'\b\u2713', fg='green', nl=False)
        click.echo('\033[?25h\n', nl=False)

    if ctx.meta['verbose'] > 1:
        click.echo(u'[{:2.4f}] Encoding training set'.format(time.time() - st_time, im))
    # don't encode test set as the alphabets may not match causing encoding failures
    gt_set.encode(codec)
    # encode without codec
    test_set.training_set = zip(test_set._images, test_set._gt)

    if load:
        if ctx.meta['verbose'] > 0:
            click.echo(u'[{:2.4f}] Loading existing model from {} '.format(time.time() - st_time, load))
        else:
            spin('Loading model')

        nn = vgsl.TorchVGSLModel.load_model(load)

        if not ctx.meta['verbose'] > 0:
            click.secho(u'\b\u2713', fg='green', nl=False)
            click.echo('\033[?25h\n', nl=False)
    else:
        if ctx.meta['verbose'] > 0:
            click.echo(u'[{:2.4f}] Creating new model {} with {} outputs'.format(time.time() - st_time, spec, len(gt_set.alphabet)))
        else:
            spin('Initializing model')

        # append output definition to spec
        spec = '[{} O1c{}]'.format(spec[1:-1], len(gt_set.codec))
        nn = vgsl.TorchVGSLModel(spec)
        # initialize weights
        nn.init_weights()
        # initialize codec
        nn.add_codec(gt_set.codec)
        # set mode to trainindg
        nn.train()

        if not ctx.meta['verbose']:
            click.secho(u'\b\u2713', fg='green', nl=False)
            click.echo('\033[?25h\n', nl=False)

    if ctx.meta['verbose'] > 0:
        click.echo(u'[{:2.4f}] Constructing optimizer (lr: {}, weight decay: {})'.format(time.time() - st_time, lrate, wdecay))

    rec = models.TorchSeqRecognizer(nn, train=True)
    optimizer = Adam(nn.nn.parameters(), lr=lrate, weight_decay=wdecay)
    #optimizer = SGD(nn.nn.parameters(), lr=lrate, momentum=0.9)

    for epoch in range(epochs):
        for trial, (input, target) in enumerate(train_loader):
            input, target = Variable(input), Variable(target)
            o = nn.nn(input)
            # height should be 1 by now
            if o.size(2) != 1:
                raise KrakenInputException('Expected dimension 3 to be 1, actual {}'.format(output.size()))
            o = o.squeeze(2)
            optimizer.zero_grad()
            loss = nn.criterion(o, target)
            loss.backward()
            optimizer.step()
            spin('Training')
        if epoch and not epoch % savefreq:
            try:
                nn.save_model('{}_{}.mlmodel'.format(output, epoch))
            except Exception as e:
                click.echo('Saving model failed: {}'.format(str(e)))
            if ctx.meta['verbose'] > 0:
                click.echo('')
                click.echo(u'[{:2.4f}] Saving to {}_{}'.format(time.time() - st_time, output, epoch))
        if epoch and not epoch % report:
            c, e = compute_error(rec, test_set.training_set)
            if ctx.meta['verbose'] < 3:
                click.echo('')
            click.echo(u'[{:2.4f}] Accuracy report ({}) {:0.4f} {} {}'.format(time.time() - st_time, epoch, (c-e)/c, c, e))


@cli.command('extract')
@click.pass_context
@click.option('-u', '--normalization',
              type=click.Choice(['NFD', 'NFKD', 'NFC', 'NFKC']), default=None,
              help='Normalize ground truth')
@click.option('-n', '--reorder/--no-reorder', default=True,
              help='Reorder transcribed lines to display order')
@click.option('-r', '--rotate/--no-rotate', default=True,
              help='Skip rotation of vertical lines')
@click.option('-o', '--output', type=click.Path(), default='training',
              help='Output directory')
@click.argument('transcribs', nargs=-1, type=click.File(lazy=True))
def extract(ctx, normalization, reorder, rotate, output, transcribs):
    """
    Extracts image-text pairs from a transcription environment created using
    ``ketos transcrib``.
    """
    st_time = time.time()
    try:
        os.mkdir(output)
    except:
        pass
    idx = 0
    manifest = []
    for fp in transcribs:
        if ctx.meta['verbose'] > 0:
            click.echo(u'[{:2.4f}] Reading {}'.format(time.time() - st_time, fp.name))
        else:
            spin('Reading transcription')
        doc = html.parse(fp)
        etree.strip_tags(doc, etree.Comment)
        td = doc.find(".//meta[@itemprop='text_direction']")
        if td is not None:
            td = td.attrib['content']
        else:
            td = 'horizontal-tb'

        im = None
        for section in doc.xpath('//section'):
            img = section.xpath('.//img')[0].get('src')
            fd = BytesIO(base64.b64decode(img.split(',')[1]))
            im = Image.open(fd)
            if not im:
                if ctx.meta['verbose'] > 0:
                    click.echo(u'[{:2.4f}] Skipping {} because image not found'.format(time.time() - st_time, fp.name))
                break
            for line in section.iter('li'):
                if line.get('contenteditable') and u''.join(line.itertext()):
                    l = im.crop([int(x) for x in line.get('data-bbox').split(',')])
                    if rotate and td.startswith('vertical'):
                        im.rotate(90, expand=True)
                    l.save('{}/{:06d}.png'.format(output, idx))
                    manifest.append('{:06d}.png'.format(idx))
                    text = u''.join(line.itertext())
                    if normalization:
                        text = unicodedata.normalize(normalization, text)
                    with open('{}/{:06d}.gt.txt'.format(output, idx), 'wb') as t:
                        if reorder:
                            t.write(get_display(text).encode('utf-8'))
                        else:
                            t.write(text.encode('utf-8'))
                    idx += 1
    if ctx.meta['verbose'] > 0:
        click.echo(u'[{:2.4f}] Extracted {} lines'.format(time.time() - st_time, idx))
    with open('{}/manifest.txt'.format(output), 'w') as fp:
        fp.write('\n'.join(manifest))
    if not ctx.meta['verbose']:
        click.secho(u'\b\u2713', fg='green', nl=False)
        click.echo('\033[?25h\n', nl=False)


@cli.command('transcrib')
@click.pass_context
@click.option('-d', '--text-direction', default='horizontal-tb',
              type=click.Choice(['horizontal-tb', 'vertical-lr', 'vertical-rl']),
              help='Sets principal text direction')
@click.option('--scale', default=None, type=click.FLOAT)
@click.option('-m', '--maxcolseps', default=2, type=click.INT)
@click.option('-b/-w', '--black_colseps/--white_colseps', default=False)
@click.option('-f', '--font', default='',
              help='Font family to use')
@click.option('-fs', '--font-style', default=None,
              help='Font style to use')
@click.option('-p', '--prefill', default=None,
              help='Use given model for prefill mode.')
@click.option('-o', '--output', type=click.File(mode='wb'), default='transcrib.html',
              help='Output file')
@click.argument('images', nargs=-1, type=click.File(mode='rb', lazy=True))
def transcription(ctx, text_direction, scale, maxcolseps, black_colseps, font,
                  font_style, prefill, output, images):
    st_time = time.time()
    ti = transcrib.TranscriptionInterface(font, font_style)

    if prefill:
        if ctx.meta['verbose'] > 0:
            click.echo(u'[{:2.4f}] Loading model {}'.format(time.time() - st_time, prefill))
        else:
            spin('Loading RNN')
        prefill = models.load_any(prefill)
        if not ctx.meta['verbose']:
            click.secho(u'\b\u2713', fg='green', nl=False)
            click.echo('\033[?25h\n', nl=False)

    for fp in images:
        if ctx.meta['verbose'] > 0:
            click.echo(u'[{:2.4f}] Reading {}'.format(time.time() - st_time, fp.name))
        else:
            spin('Reading images')
        im = Image.open(fp)
        if not binarization.is_bitonal(im):
            if ctx.meta['verbose'] > 0:
                click.echo(u'[{:2.4f}] Binarizing page'.format(time.time() - st_time))
            im = binarization.nlbin(im)
        if ctx.meta['verbose'] > 0:
            click.echo(u'[{:2.4f}] Segmenting page'.format(time.time() - st_time))
        res = pageseg.segment(im, text_direction, scale, maxcolseps, black_colseps)
        if prefill:
            it = rpred.rpred(prefill, im, res)
            preds = []
            for pred in it:
                if ctx.meta['verbose'] > 0:
                    click.echo(u'[{:2.4f}] {}'.format(time.time() - st_time, pred.prediction))
                else:
                    spin('Recognizing')
                preds.append(pred)
            if ctx.meta['verbose'] > 0:
                click.echo(u'Execution time: {}s'.format(time.time() - st_time))
            else:
                click.secho(u'\b\u2713', fg='green', nl=False)
                click.echo('\033[?25h\n', nl=False)
            ti.add_page(im, records=preds)
        else:
            ti.add_page(im, res)
        fp.close()
    if not ctx.meta['verbose']:
        click.secho(u'\b\u2713', fg='green', nl=False)
        click.echo('\033[?25h\n', nl=False)
    if ctx.meta['verbose'] > 0:
        click.echo(u'[{:2.4f}] Writing transcription to {}'.format(time.time() - st_time, output.name))
    else:
        spin('Writing output')
    ti.write(output)
    if not ctx.meta['verbose']:
        click.secho(u'\b\u2713', fg='green', nl=False)
        click.echo('\033[?25h\n', nl=False)


@cli.command('linegen')
@click.pass_context
@click.option('-f', '--font', default='sans',
              help='Font family to render texts in.')
@click.option('-n', '--maxlines', type=click.INT, default=0,
              help='Maximum number of lines to generate')
@click.option('-e', '--encoding', default='utf-8',
              help='Decode text files with given codec.')
@click.option('-u', '--normalization',
              type=click.Choice(['NFD', 'NFKD', 'NFC', 'NFKC']), default=None,
              help='Normalize ground truth')
@click.option('-ur', '--renormalize',
              type=click.Choice(['NFD', 'NFKD', 'NFC', 'NFKC']), default=None,
              help='Renormalize text for rendering purposes.')
@click.option('--reorder/--no-reorder', default=False, help='Reorder code points to display order')
@click.option('-fs', '--font-size', type=click.INT, default=32,
              help='Font size to render texts in.')
@click.option('-fw', '--font-weight', type=click.INT, default=400,
              help='Font weight to render texts in.')
@click.option('-l', '--language',
              help='RFC-3066 language tag for language-dependent font shaping')
@click.option('-ll', '--max-length', type=click.INT, default=None,
              help="Discard lines above length (in Unicode codepoints).")
@click.option('--strip/--no-strip', help="Remove whitespace from start and end "
              "of lines.")
@click.option('-d', '--disable-degradation', is_flag=True, help='Dont degrade '
              'output lines.')
@click.option('-b/-nb', '--binarize/--no-binarize', default=True,
              help='Binarize output using nlbin.')
@click.option('-m', '--mean', type=click.FLOAT, default=0.0,
              help='Mean of distribution to take means for gaussian noise '
              'from.')
@click.option('-s', '--sigma', type=click.FLOAT, default=0.001,
              help='Mean of distribution to take standard deviation values for '
              'Gaussian noise from.')
@click.option('-r', '--density', type=click.FLOAT, default=0.002,
              help='Mean of distribution to take density values for S&P noise '
              'from.')
@click.option('-d', '--distort', type=click.FLOAT, default=1.0,
              help='Mean of distribution to take distortion values from')
@click.option('-ds', '--distortion-sigma', type=click.FLOAT, default=20.0,
              help='Mean of distribution to take standard deviations for the '
              'Gaussian kernel from')
@click.option('--legacy/--no-legacy', default=False,
              help='Use ocropy-style degradations')
@click.option('-o', '--output', type=click.Path(), default='training_data',
              help='Output directory')
@click.argument('text', nargs=-1, type=click.Path(exists=True))
def line_generator(ctx, font, maxlines, encoding, normalization, renormalize,
                   reorder, font_size, font_weight, language, max_length, strip,
                   disable_degradation, binarize, mean, sigma, density,
                   distort, distortion_sigma, legacy, output, text):
    """
    Generates artificial text line training data.
    """
    lines = set()
    if not text:
        return
    st_time = time.time()
    for t in text:
        with click.open_file(t, encoding=encoding) as fp:
            if ctx.meta['verbose'] > 0:
                click.echo(u'[{:2.4f}] Reading {}'.format(time.time() - st_time, t))
            else:
                spin('Reading texts')
            lines.update(fp.readlines())
    if normalization:
        lines = set([unicodedata.normalize(normalization, line) for line in lines])
    if strip:
        lines = set([line.strip() for line in lines])
    if max_length:
        lines = set([line for line in lines if len(line) < max_length])
    if ctx.meta['verbose'] > 0:
        click.echo(u'[{:2.4f}] Read {} lines'.format(time.time() - st_time, len(lines)))
    else:
        click.secho(u'\b\u2713', fg='green', nl=False)
        click.echo('\033[?25h\n', nl=False)
        click.echo('Read {} unique lines'.format(len(lines)))
    if maxlines and maxlines < len(lines):
        click.echo('Sampling {} lines\t'.format(maxlines), nl=False)
        lines = list(lines)
        lines = [lines[idx] for idx in np.random.randint(0, len(lines), maxlines)]
        click.secho(u'\u2713', fg='green')
    try:
        os.makedirs(output)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    lines = [line.strip() for line in lines]

    # calculate the alphabet and print it for verification purposes
    alphabet = set()
    for line in lines:
        alphabet.update(line)
    chars = []
    combining = []
    for char in sorted(alphabet):
        if unicodedata.combining(char):
            combining.append(unicodedata.name(char))
        else:
            chars.append(char)
    click.echo(u'Σ (len: {})'.format(len(alphabet)))
    click.echo(u'Symbols: {}'.format(''.join(chars)))
    if combining:
        click.echo(u'Combining Characters: {}'.format(', '.join(combining)))
    lg = linegen.LineGenerator(font, font_size, font_weight, language)
    for idx, line in enumerate(lines):
        if ctx.meta['verbose'] > 0:
            click.echo(u'[{:2.4f}] {}'.format(time.time() - st_time, line))
        else:
            spin('Writing images')
        try:
            if renormalize:
                im = lg.render_line(unicodedata.normalize(renormalize, line))
            else:
                im = lg.render_line(line)
        except KrakenCairoSurfaceException as e:
            if ctx.meta['verbose'] > 0:
                click.echo('[{:2.4f}] {}: {} {}'.format(time.time() - st_time, e.message, e.width, e.height))
            else:
                click.secho(u'\b\u2717', fg='red')
                click.echo('{}: {} {}'.format(e.message, e.width, e.height))
            continue
        if not disable_degradation and not legacy:
            im = linegen.distort_line(im, abs(np.random.normal(distort)), abs(np.random.normal(distortion_sigma)))
            im = linegen.degrade_line(im, abs(np.random.normal(mean)), abs(np.random.normal(sigma)), abs(np.random.normal(density)))
        elif legacy:
            im = linegen.ocropy_degrade(im)
        if binarize:
            try:
                im = binarization.nlbin(im)
            except KrakenInputException as e:
                click.echo('{}'.format(e.message))
                continue
        im.save('{}/{:06d}.png'.format(output, idx))
        with open('{}/{:06d}.gt.txt'.format(output, idx), 'wb') as fp:
            if reorder:
                fp.write(get_display(line).encode('utf-8'))
            else:
                fp.write(line.encode('utf-8'))
    if ctx.meta['verbose'] == 0:
        click.secho(u'\b\u2713', fg='green', nl=False)
        click.echo('\033[?25h\n', nl=False)


if __name__ == '__main__':
    cli()
