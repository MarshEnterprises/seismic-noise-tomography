#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
Created on Tue Apr 10 21:57:12 2018

@author: daq7

edited by benjamin whitehead on 2022-05-18
"""
import sys
import pickle
import matplotlib.pyplot as plt
import matplotlib.cm as cm
#from pysismo.psutils import resample
import numpy as np

#def plot_xcorrs(path=sys.argv[1], stripheight=0.5, samplerate=False):
def plot_xcorrs(path, samplerate, stripheight=0.5):
    '''
    this function plots the cross correlations as coloured strips by inter station distance.
    It is quite inefficient, but it does the job ... eventually. The problem is that the code
    relies on plotting filled areas in loops, written entirely in python. No suitable matplotlib
    graph exists as far as I can tell.
    path (str): path to pickle file
    stripheight (float): sets the height of the strips in km
    samplerate (float): resample correlations to a period of samplerate
    '''

    xc = pickle.load(open(path,"rb"))

    pairs = xc.pairs()
    maxdist = max(xc[x][y].dist() for (x,y) in pairs)
    mindist = min(xc[x][y].dist() for (x,y) in pairs)
    corrlength = max(len(xc[x][y].dataarray) for (x,y) in pairs)

    #pairs = pairs[0:3]
    for ipair, (s1, s2) in enumerate(pairs):
        dataar = xc[s1][s2].dataarray
        dataar = dataar/(max(abs(dataar)))
        timear = xc[s1][s2].timearray

        if samplerate:
            # resample to speed up plotting
            ntimear = np.linspace(min(timear), max(timear), (max(timear)-min(timear))/samplerate)
            dataar = np.interp(ntimear, timear, dataar)
            timear = ntimear
        else:
            samplerate = timear[1]-timear[0]

        dist = xc[s1][s2].dist()
        print(str(ipair) + ': drawing ' + str(s1) + '-' + str(s2))


        for i in range(len(timear)):
            fill_colour = cm.seismic(0.5 + dataar[i]/2)

            # the commented is too processing intensive
            #plt.fill_between(x=(timear[i]-samplerate/2, timear[i]+samplerate/2), y1=dist+stripheight,
            #                 y2=dist-stripheight, color=fill_colour)

            if dataar[i] < 0.15 or dataar[i] > 0.85:
                plt.scatter(timear[i], dist, facecolor=fill_colour, marker='|')

    plt.xlabel('seconds')
    plt.ylabel('inter-station distance (km)')

    plt.show()


plot_xcorrs(path='../output/cross-correlation/xcorr_2015-2015_xmlresponse_0.5-12.5.pickle', samplerate=0.4,
            stripheight=0.2)