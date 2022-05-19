#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
Created on Tue Apr 10 21:57:12 2018

@author: daq7
"""

import pickle
from pysismo import psutils
import matplotlib.pyplot as plt
import numpy as np
import math as m

#xc = pickle.load(open("../output/cross-correlation/xcorr_2015-2015_xmlresponse_0.16-1.25.pickle","rb"))
xc = pickle.load(open("../output/cross-correlation/xcorr_2015-2015_xmlresponse_0.5-12.5.pickle","rb"))
plt.figure()
pairs = xc.pairs()
maxdist = max(xc[x][y].dist() for (x,y) in pairs)
mindist = min(xc[x][y].dist() for (x,y) in pairs)
maxtime = max(max(xc[x][y].timearray) for (x,y) in pairs)
mintime = min(min(xc[x][y].timearray) for (x,y) in pairs)
corrlength = max(len(xc[x][y].dataarray) for (x,y) in pairs)

stripheight = 0.2
#pairs.sort(key=lambda (s1,s2): xc[s1][s2].dist())

Y = np.zeros(shape=(int(m.ceil(maxdist/stripheight))+1,corrlength))
pairs.sort(key=lambda (s1,s2): xc[s1][s2].dist())

for ipairs, (s1, s2) in enumerate(pairs):
    #data = psutils.bandpass_butterworth(data=xc[s1][s2].dataarray,dt=1.0,periodmin=8.0,periodmax=40.0)
    data = xc[s1][s2].dataarray / (max(abs(xc[s1][s2].dataarray)))
    ypos = int(round(xc[s1][s2].dist()/stripheight))
    Y[ypos] = data

plt.imshow(Y, vmin=-1., vmax=1., cmap='seismic', aspect='auto', origin='lower', extent=(mintime,maxtime,0,maxdist+stripheight))
plt.xlim(-40,40)
plt.xlabel('time (s)')
plt.ylabel('inter-station distance (km)')
plt.show()

