#!/usr/bin/env python

import pickle
import numpy as np
import matplotlib.pyplot as plt

xc = pickle.load(open('../output/cross-correlation/xcorr_2015-2015_xmlresponse_LG_d7_f1-5_e1-45.pickle'))

weekstacks = [] 

for cp in xc.ACF.ACG.controlxcs: 
    weekstacks = weekstacks + [cp.dataarray]


vstack = np.vstack(weekstacks) 
stdev = np.std(vstack, axis=0)

fig, ax = plt.subplots()

ax.plot(xc.ACF.ACG.dataarray, color='black', lw=2)

for ws in xc.ACF.ACG.controlxcs:
    ax.plot(ws.dataarray)
    

# ax.plot(stdev, color='red', lw=2)

plt.show()


plt.show()

