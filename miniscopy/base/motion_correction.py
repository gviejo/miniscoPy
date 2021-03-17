# -*- coding: utf-8 -*-
"""


The functions apply_shifts_dft, register_translation, _compute_error, _compute_phasediff, and _upsampled_dft are from
SIMA (https://github.com/losonczylab/sima), licensed under the  GNU GENERAL PUBLIC LICENSE, Version 2, 1991.
These same functions were adapted from sckikit-image, licensed as follows:

Copyright (C) 2011, the scikit-image team
 All rights reserved.

 Redistribution and use in source and binary forms, with or without
 modification, are permitted provided that the following conditions are
 met:

  1. Redistributions of source code must retain the above copyright
     notice, this list of conditions and the following disclaimer.
  2. Redistributions in binary form must reproduce the above copyright
     notice, this list of conditions and the following disclaimer in
     the documentation and/or other materials provided with the
     distribution.
  3. Neither the name of skimage nor the names of its contributors may be
     used to endorse or promote products derived from this software without
     specific prior written permission.

 THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
 IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 DISCLAIMED. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT,
 INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
 (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
 SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
 HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
 STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
 IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 POSSIBILITY OF SUCH DAMAGE.



"""
import numpy as np
import cv2
import itertools
#from . import sima_functions as sima 
import warnings
import pandas as pd
import re
import av
from tqdm import tqdm
import os
import sys
import h5py as hd
from IPython.core.debugger import Pdb
from copy import copy
import numba
from numba import jit
from numba import cuda
from miniscopy.base.sima_functions import *
from time import time
from scipy.interpolate import interp2d
from scipy.signal import fftconvolve
import cupy as cp


def get_vector_field_image (folder_name,shift_appli, parameters):
    '''This function is based on the jupyter notebook main_test_motion_correction.ipynb
    parameters:
    -folder_name : str, the name of the folder where is the inital movie
    -shift_appli : ndarry, the shift applied to the template in oder to get the shifted image
    -parameters : dict'''

    import matplotlib.pyplot as plt
    import matplotlib.patches as patches
    import glob

    files = glob.glob(folder_name+'/*.avi')
    if len(files) == 0:
        import urllib.request
        url = "https://www.dropbox.com/s/mujmgcwkit52xpn/A0608_msCam1.avi?dl=1"
        with urllib.request.urlopen(url) as response, open(folder_name +  '/A0608_msCam1.avi', 'wb') as out_file:
            data = response.read()
            out_file.write(data)
        files= glob.glob(folder_name+'/*.avi')
        if len(files) == 0:
            print("No avi files found, please provide one at least")
            sys.exit(-1)
    video_info, videos, dims = get_video_info(files)
    hdf_mov2 = get_hdf_file(videos, video_info, dims,parameters['save_original'])
    movie = hdf_mov2['movie']
    template1 = movie[0].copy()
    template = template1.reshape(dims)
    max_fluo_template, mf_template = get_max_fluo(template,parameters)
    top,bottom, left, right = max_fluo_template[0]-50,max_fluo_template[0]+50,max_fluo_template[1]-50,max_fluo_template[1]+50
    rect = patches.Rectangle((left,top),100,100,linewidth=1,edgecolor='r',facecolor='none',label = 'shift area')
    image =  template.copy()
    image[top : bottom, left:right] = image[top + shift_appli[0] : bottom + shift_appli[0], left+ shift_appli[1]: right + shift_appli[1]]
    max_fluo_image, mf_image = get_max_fluo(image,parameters)
    patches_index, wdims, pdims    = get_patches_position(dims, **parameters)
    shifts_patch = np.zeros((len(patches_index),2))
    for i,patch_pos in enumerate(patches_index):
        xs, xe, ys, ye = (patch_pos[0],np.minimum(patch_pos[0]+wdims[0],dims[0]-1),patch_pos[1],np.minimum(patch_pos[1]+wdims[1],dims[1]-1)) # s = start, e = exit
        filtered_image = low_pass_filter_space(image[xs:xe,ys:ye].copy(), parameters['filter_size_patch'])
        filtered_template = low_pass_filter_space(template[xs:xe,ys:ye].copy(), parameters['filter_size_patch'])
        shifts_patch[i], error, phasediff = register_translation(filtered_template, filtered_image, parameters['upsample_factor'],"real",None,None, parameters['max_shifts']) #coordinate given back in order Y,X
    shift_img_x     = shifts_patch[:,0].reshape(pdims)
    shift_img_y     = shifts_patch[:,1].reshape(pdims) 
    new_overlaps    = parameters['overlaps']
    new_strides     = tuple(np.round(np.divide(parameters['strides'], parameters['upsample_factor_grid'])).astype(np.int))
    upsamp_patches_index, upsamp_wdims, upsamp_pdims = get_patches_position(dims, new_strides, new_overlaps)
    shift_img_x     = cv2.resize(shift_img_x, (upsamp_pdims[1],upsamp_pdims[0]), interpolation = cv2.INTER_CUBIC)
    shift_img_y     = cv2.resize(shift_img_y, (upsamp_pdims[1],upsamp_pdims[0]), interpolation = cv2.INTER_CUBIC)
    X,Y,U,V,Xp,Yp = vector_field(shift_img_x,shift_img_y,new_strides,upsamp_wdims,upsamp_pdims,dims)
    
    return (image,X, Y, U, V,Xp,Yp,rect,dims)


def vector_field (matrix_X, matrix_Y,strides,wdims,pdims,dims):
    """
    Creat a vector field from 2 matrix of coordinates
    
    parameters : 
    -matrix_X = matrix of height coordinates of the vector field
    -matrix_Y = matrix of weight coordinates of the vector field
    -strides = np.array, (top,left) coordinates of each patch
    -wdims = np.array, dimension of each patch (h,w)
    -dims = dimension of the image
    -pdims = np.array, (number of patches on heigt, number of patches on weight)"""
    x= np.zeros(pdims[1])
    y= np.zeros(pdims[0])
    for i in range(0,pdims[1]):
        x[i]= np.minimum(strides[1]*i + wdims[1]/2, dims[1])
    for j in range(0,pdims[0]):
        y[j]= np.minimum(strides[0]*j + wdims[0]/2, dims[0])
    X,Y = np.meshgrid(x,y)

    X_flat = np.ravel(X.copy())
    Y_flat = np.ravel(Y.copy())
    U_flat= np.ravel(matrix_Y.copy())
    V_flat = np.ravel(matrix_X.copy())
    xp = np.zeros(U_flat.shape)
    yp = np.zeros(V_flat.shape) 
    xp.fill(np.nan)
    yp.fill(np.nan)
    for i, uf in enumerate(U_flat): 
        if uf == 0 and V_flat[i] == 0 : # if there is no shift
            U_flat[i] = None
            V_flat[i] = None
            xp[i] = X_flat[i]
            yp[i] = Y_flat[i]

    U = U_flat.reshape(pdims)
    V = V_flat.reshape(pdims)
    Xp = xp.reshape(pdims)
    Yp = yp.reshape(pdims)

    return (X,Y,U,V,Xp,Yp)


def join_patches(image,max_shear,upsamp_wdims,new_overlaps,upsamp_pdims,upsamp_patches_index,patch_pos,total_shifts,new_upsamp_patches):
    
    normalizer      = np.zeros_like(image)*np.nan
    new_image = image.copy()

    if max_shear < 0.5:
        np.seterr(divide='ignore')
        # create weight matrix for blending
        # different from original.    
        tmp             = np.ones(upsamp_wdims)    
        tmp[:new_overlaps[0], :] = np.linspace(0, 1, new_overlaps[0])[:, None]
        tmp             = tmp*np.flip(tmp, 0)
        tmp2             = np.ones(upsamp_wdims)    
        tmp2[:, :new_overlaps[1]] = np.linspace(0, 1, new_overlaps[1])[None, :]
        tmp2            = tmp2*np.flip(tmp2, 1)
        blending_func   = tmp*tmp2
        border          = tuple(itertools.product(np.arange(upsamp_pdims[0]),np.arange(upsamp_wdims[0])))
        for i, patch_pos in enumerate(upsamp_patches_index):
            xs, xe, ys, ye = (patch_pos[0],patch_pos[0]+upsamp_wdims[0],patch_pos[1],patch_pos[1]+upsamp_wdims[1])        
            ye = np.minimum(ye, new_image.shape[1])
            xe = np.minimum(xe, new_image.shape[0])
            prev_val_1  = normalizer[xs:xe,ys:ye]
            prev_val    = new_image[xs:xe,ys:ye]

            tmp = new_upsamp_patches[i,xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]
            tmp2 = blending_func[xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]

            if xs == 0 or ys == 0 or xe == new_image.shape[0] or ye == new_image.shape[1]:
                normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(tmp)*1*np.ones_like(tmp2), prev_val_1]),-1)                    
                new_image[xs:xe,ys:ye] = np.nansum(np.dstack([tmp*np.ones_like(tmp2), prev_val]),-1)
            else:
                normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(tmp)*1*tmp2, prev_val_1]),-1)
                new_image[xs:xe,ys:ye] = np.nansum(np.dstack([tmp*tmp2, prev_val]),-1)

        new_image = new_image/normalizer

    else:

        half_overlap_x = np.int(new_overlaps[0] / 2)
        half_overlap_y = np.int(new_overlaps[1] / 2)        
        for i, patch_pos in enumerate(upsamp_patches_index):
            if total_shifts[i].sum() != 0.0 : 
                if patch_pos[0] == 0 and patch_pos[1] == 0:
                    xs = patch_pos[0]
                    xe = patch_pos[0]+upsamp_wdims[0]-half_overlap_x
                    ys = patch_pos[1]
                    ye = patch_pos[1]+upsamp_wdims[1]-half_overlap_y            
                elif patch_pos[0] == 0:
                    xs = patch_pos[0]
                    xe = patch_pos[0]+upsamp_wdims[0]-half_overlap_x
                    ys = patch_pos[1]+half_overlap_y
                    ye = patch_pos[1]+upsamp_wdims[1]-half_overlap_y                
                    ye = np.minimum(ye, new_image.shape[1])
                elif patch_pos[1] == 0:
                    xs = patch_pos[0]+half_overlap_x
                    xe = patch_pos[0]+upsamp_wdims[0]-half_overlap_x
                    xe = np.minimum(xe, new_image.shape[0])
                    ys = patch_pos[1]
                    ye = patch_pos[1]+upsamp_wdims[1]-half_overlap_y                                
                else:
                    xs = patch_pos[0]+half_overlap_x
                    xe = patch_pos[0]+upsamp_wdims[0]-half_overlap_x
                    xe = np.minimum(xe, new_image.shape[0])
                    ys = patch_pos[1]+half_overlap_y
                    ye = patch_pos[1]+upsamp_wdims[1]-half_overlap_y                                
                    ye = np.minimum(ye, new_image.shape[1])
                new_image[xs:xe,ys:ye] = new_upsamp_patches[i,xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]
    
    return(new_image)



def get_max_fluo(img, parameters) :
    """ Return the position and value of the maximum of fluorescence of an image 
    
    Parameters : 
    -img : ndarray, the image where you want to detect the fluorescence 
    
    Returns : 
    -max_fluo : tuple, position of the maximum of fluorescence 
    -mf : float,  value of the maximum of fluorescence"""

    filtered_img = low_pass_filter_space(img.copy(), parameters['filter_size_patch'])
    mf = cv2.minMaxLoc(filtered_img)[1] # Value of the maximum of fluo
    max_fluo = cv2.minMaxLoc(filtered_img)[3] # Position of the maximum of fluo (w,h)
    max_fluo = max_fluo[::-1] # (h,w) 

    
    return max_fluo, mf 

def get_patches_position(dims, strides, overlaps, **kwargs):
    ''' Return a matrix of the position of each patches without overlapping, the dimension of each patch and the dimension of this matrix 

    Positional arguments :
    -dims : dimension of the template image
    -strides : the dimension of each patches without overlapping
    -overlaps : dimension of overlaps'''

    wdims       = tuple(np.add(strides, overlaps)) #dimension of patches
    # different from caiman implemantion
    height_pos  = np.arange(0, dims[0], strides[0])
    width_pos   = np.arange(0, dims[1], strides[1])
    patches_index = np.atleast_3d(np.meshgrid(height_pos, width_pos, indexing = 'ij'))
    pdims       = patches_index.shape[1:] #dimension of the patches index 
    return patches_index.reshape(patches_index.shape[0], np.prod(patches_index.shape[1:])).transpose(), wdims, pdims


def apply_shift_iteration(img, shift, border_nan=False, border_type=cv2.BORDER_REFLECT):
    """Applied an affine transformation to an image
    
    Parameters:
    -img : ndarray, image to be transformed
    -shift: ndarray, (h,w), the shift to be applied to the original image
    -border_nan : how to deal with the borders
    -border_type : pixel extrapolation method 
    
    Returns:
    - img : ndarray, image transformed"""


    sh_x_n, sh_y_n = shift
    w_i, h_i = img.shape
    M = np.float32([[1, 0, sh_y_n], [0, 1, sh_x_n]])    
    min_, max_ = np.min(img), np.max(img)
    img = np.clip(cv2.warpAffine(img, M, (h_i, w_i), flags = cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT), min_, max_)
    if border_nan:
        max_w, max_h, min_w, min_h = 0, 0, 0, 0
        max_h, max_w = np.ceil(np.maximum((max_h, max_w), shift)).astype(np.int)
        min_h, min_w = np.floor(np.minimum((min_h, min_w), shift)).astype(np.int)
        img[:max_h, :] = np.nan
        if min_h < 0:
            img[min_h:, :] = np.nan
        img[:, :max_w] = np.nan
        if min_w < 0:
            img[:, min_w:] = np.nan
    img[np.isinf(img)] = np.nan
    img[np.isnan(img)] = np.nanmean(img)

    return img


def tile_and_correct(image, template, dims, parameters):
    """ perform piecewise rigid motion correction iteration, by
        1) dividing the FOV in patches
        2) motion correcting each patch separately
        3) upsampling the motion correction vector field
        4) stiching back together the corrected subpatches"""            
        
    image           = image.reshape(dims)    
    template_uncrop = template.copy()
    template        = template.reshape(dims)

    # extract patches positions
    patches_index, wdims, pdims    = get_patches_position(dims, **parameters)

    # extract shifts for each patch
    shifts_patch = np.zeros((len(patches_index),2))
    for i,patch_pos in enumerate(patches_index):
        xs, xe, ys, ye = (patch_pos[0],np.minimum(patch_pos[0]+wdims[0],dims[0]-1),patch_pos[1],np.minimum(patch_pos[1]+wdims[1],dims[1]-1)) # s = start, e = exit
        filtered_image = low_pass_filter_space(image[xs:xe,ys:ye].copy(), parameters['filter_size_patch'])
        filtered_template = low_pass_filter_space(template[xs:xe,ys:ye].copy(), parameters['filter_size_patch'])
        shifts_patch[i], error, phasediff = register_translation(filtered_template, filtered_image, parameters['upsample_factor'],"real",None,None, parameters['max_shifts']) #coordinate given back in order Y,X

    # create a vector field    
    shift_img_x     = shifts_patch[:,0].reshape(pdims)
    shift_img_y     = shifts_patch[:,1].reshape(pdims) 


    # upsampling 
    new_overlaps    = parameters['overlaps']
    new_strides     = tuple(np.round(np.divide(parameters['strides'], parameters['upsample_factor_grid'])).astype(np.int))
    upsamp_patches_index, upsamp_wdims, upsamp_pdims = get_patches_position(dims, new_strides, new_overlaps)

    # resize shift_img_
    shift_img_x     = cv2.resize(shift_img_x, (upsamp_pdims[1],upsamp_pdims[0]), interpolation = cv2.INTER_CUBIC)
    shift_img_y     = cv2.resize(shift_img_y, (upsamp_pdims[1],upsamp_pdims[0]), interpolation = cv2.INTER_CUBIC)

    #create vector field
    x= np.zeros(upsamp_pdims[1])
    y= np.zeros(upsamp_pdims[0])
    for i in range(0,upsamp_pdims[1]):
        x[i]= new_strides[1]*i + upsamp_wdims[1]/2
    for j in range(0,upsamp_pdims[0]):
        y[j]= new_strides[0]*j + upsamp_wdims[0]/2
    X,Y = np.meshgrid(x,y)

    U_flat= np.ravel(shift_img_y.copy())
    V_flat = np.ravel(shift_img_x.copy())
    for i, uf in enumerate(U_flat):
        if uf == 0 and V_flat[i] == 0 :
            U_flat[i] = None
            V_flat[i] = None

        
    U = U_flat.reshape(upsamp_pdims)
    V = V_flat.reshape(upsamp_pdims)

    #apply shift iteration
    num_tiles           = np.prod(upsamp_pdims) #number of patches
    max_shear           = np.percentile([np.max(np.abs(np.diff(ssshh, axis=xxsss))) for ssshh, xxsss in itertools.product([shift_img_x, shift_img_y], [0, 1])], 75)    
    total_shifts        = np.vstack((shift_img_x.flatten(),shift_img_y.flatten())).transpose()
    new_upsamp_patches  = np.ones((num_tiles, upsamp_wdims[0], upsamp_wdims[1]))*np.inf
    for i, patch_pos in enumerate(upsamp_patches_index):
        xs, xe, ys, ye  = (patch_pos[0],np.minimum(patch_pos[0]+upsamp_wdims[0],dims[0]),patch_pos[1],np.minimum(patch_pos[1]+upsamp_wdims[1],dims[1]))
        patch           = image[xs:xe,ys:ye]
        if total_shifts[i].sum():#where there is a shift                        
            new_upsamp_patches[i,0:patch.shape[0],0:patch.shape[1]] = apply_shift_iteration(patch.copy(), total_shifts[i], border_nan = True)
        else:
            new_upsamp_patches[i,0:patch.shape[0],0:patch.shape[1]] = patch.copy()


    normalizer      = np.ones_like(image)
    new_image       = np.copy(image)    
    med             = np.median(new_image)

    if np.all(shift_img_x == 0) and np.all(shift_img_y == 0):
        return new_image.flatten()
    else:
        if max_shear < 0.5:                        
            np.seterr(all='raise')
            # create weight matrix for blending
            # different from original.    
            # plus the blending func is dependant of the border.

            tmp             = np.ones(upsamp_wdims)    
            tmp[:new_overlaps[0], :] = np.linspace(0, 1, new_overlaps[0])[:, None]
            corner          = np.flip(tmp, 0) * np.rot90(tmp, -1)
            tmp             = tmp*np.flip(tmp, 0)
            tmp2             = np.ones(upsamp_wdims)    
            tmp2[:, :new_overlaps[1]] = np.linspace(0, 1, new_overlaps[1])[None, :]
            tmp2            = tmp2*np.flip(tmp2, 1)
            blending_func   = tmp*tmp2        
            upper_band       = np.ones(upsamp_wdims)    
            upper_band[-new_overlaps[0]:,:] = np.linspace(1, 0, new_overlaps[0])[:, None]
            upper_band = np.rot90(upper_band, -1)*upper_band*np.rot90(upper_band, 1)
            left_band = np.rot90(upper_band, 1)

            
            for i, patch_pos in enumerate(upsamp_patches_index):            
                xs, xe, ys, ye = (patch_pos[0],patch_pos[0]+upsamp_wdims[0],patch_pos[1],patch_pos[1]+upsamp_wdims[1])        
                ye = np.minimum(ye, new_image.shape[1])
                xe = np.minimum(xe, new_image.shape[0])
                prev_norm  = np.copy(normalizer[xs:xe,ys:ye])
                prev_val    = np.copy(new_image[xs:xe,ys:ye])
                new_val = np.copy(new_upsamp_patches[i,xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]])

                if xs != 0 and ys != 0 and xe != new_image.shape[0] and ye != new_image.shape[1]:
                    tmp2 = blending_func[xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]
                    normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(new_val)*1*tmp2, prev_norm]),-1)
                    new_image[xs:xe,ys:ye] = np.nansum(np.dstack([new_val*tmp2, prev_val]),-1)
                elif xs == 0 and ys == 0: # upper left corner                
                    normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(new_val)*1*corner, prev_norm]),-1)
                    new_image[xs:xe,ys:ye] = np.nansum(np.dstack([new_val*corner, prev_val]),-1)
                elif xs == 0 and ye == new_image.shape[1]: # upper right corner
                    tmp3 = np.rot90(corner, -1)[xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]
                    normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(new_val)*1*tmp3, prev_norm]),-1)
                    new_image[xs:xe,ys:ye] = np.nansum(np.dstack([new_val*tmp3, prev_val]),-1)
                elif xe == new_image.shape[0] and ys == 0: # lower left corner                
                    tmp3 = np.rot90(corner, 1)[xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]
                    normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(new_val)*1*tmp3, prev_norm]),-1)
                    new_image[xs:xe,ys:ye] = np.nansum(np.dstack([new_val*tmp3, prev_val]),-1)
                elif xe == new_image.shape[0] and ye == new_image.shape[1]: # lower right corner
                    tmp3 = np.rot90(corner, 2)[xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]               
                    normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(new_val)*1*tmp3, prev_norm]),-1)
                    new_image[xs:xe,ys:ye] = np.nansum(np.dstack([new_val*tmp3, prev_val]),-1)
                elif xs == 0: # upper
                    tmp3 = upper_band[xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]                
                    normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(new_val)*1*tmp3, prev_norm]),-1)
                    new_image[xs:xe,ys:ye] = np.nansum(np.dstack([new_val*tmp3, prev_val]),-1)
                elif xe == new_image.shape[0]: # lower
                    tmp3 = np.flip(upper_band, 0)[xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]  
                    normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(new_val)*1*tmp3, prev_norm]),-1)
                    new_image[xs:xe,ys:ye] = np.nansum(np.dstack([new_val*tmp3, prev_val]),-1)
                elif ys == 0: # left
                    tmp3 = left_band[xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]
                    normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(new_val)*1*tmp3, prev_norm]),-1)
                    new_image[xs:xe,ys:ye] = np.nansum(np.dstack([new_val*tmp3, prev_val]),-1)
                elif ye == new_image.shape[1]: # right
                    tmp3 = np.flip(left_band, 1)[xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]
                    normalizer[xs:xe,ys:ye] = np.nansum(np.dstack([~np.isnan(new_val)*1*tmp3, prev_norm]),-1)
                    new_image[xs:xe,ys:ye] = np.nansum(np.dstack([new_val*tmp3, prev_val]),-1)
            
            
            new_image = new_image/normalizer

        else:        
            half_overlap_x = np.int(new_overlaps[0] / 2)
            half_overlap_y = np.int(new_overlaps[1] / 2)        
            for i, patch_pos in enumerate(upsamp_patches_index):
                if total_shifts[i].sum() != 0.0 :
                    if patch_pos[0] == 0 and patch_pos[1] == 0:
                        xs = patch_pos[0]
                        xe = patch_pos[0]+upsamp_wdims[0]-half_overlap_x
                        ys = patch_pos[1]
                        ye = patch_pos[1]+upsamp_wdims[1]-half_overlap_y            
                    elif patch_pos[0] == 0:
                        xs = patch_pos[0]
                        xe = patch_pos[0]+upsamp_wdims[0]-half_overlap_x
                        ys = patch_pos[1]+half_overlap_y
                        ye = patch_pos[1]+upsamp_wdims[1]-half_overlap_y                
                        ye = np.minimum(ye, new_image.shape[1])
                    elif patch_pos[1] == 0:
                        xs = patch_pos[0]+half_overlap_x
                        xe = patch_pos[0]+upsamp_wdims[0]-half_overlap_x
                        xe = np.minimum(xe, new_image.shape[0])
                        ys = patch_pos[1]
                        ye = patch_pos[1]+upsamp_wdims[1]-half_overlap_y                                
                    else:
                        xs = patch_pos[0]+half_overlap_x
                        xe = patch_pos[0]+upsamp_wdims[0]-half_overlap_x
                        xe = np.minimum(xe, new_image.shape[0])
                        ys = patch_pos[1]+half_overlap_y
                        ye = patch_pos[1]+upsamp_wdims[1]-half_overlap_y                                
                        ye = np.minimum(ye, new_image.shape[1])
                    new_patch = new_upsamp_patches[i,xs-patch_pos[0]:xe-patch_pos[0],ys-patch_pos[1]:ye-patch_pos[1]]
                    new_patch[np.isinf(new_patch)] = np.nan
                    dims_patch = new_patch.shape
                    if np.isnan(new_patch).all():
                        new_patch[:,:] = med
                    elif np.isnan(new_patch).any():
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", category=RuntimeWarning)
                            new_patch[np.isnan(new_patch)] = np.nanmedian(new_patch)
                
                    new_image[xs:xe,ys:ye] = new_patch

    if np.isinf(new_image).any(): new_image[np.isinf(new_image)] = np.nanmedian(new_image)

    return new_image.flatten()

###################################################################################
# GLOBAL CORRECTION
###################################################################################
@jit(nopython=True, parallel = True) # no big improvement here ~ 136 ms for 200 images
def pad_array(images, offset):
    """
        Numba 0.42.0 does not support padding
        Trying here in reflect mode
        images should be 3d
    """
    h = images.shape[1]
    w = images.shape[2]
    pad_images = np.zeros((images.shape[0], h+2*offset, w+2*offset), dtype = np.float32)
    for i in range(images.shape[0]):
        # image = np.zeros((h,w), dtype=np.float32)
        image = images[i]#.reshape((h,w))
        # image = image.reshape((h,w))
        tmp  = pad_images[i]
        tmp[offset:-offset,offset:-offset] = image
        tmp[0:offset,offset:-offset] = image[1:1+offset][::-1] # upper part
        tmp[-offset:,offset:-offset] = image[-offset-1:-1][::-1] # lower part
        tmp[offset:-offset,0:offset] = image[:,1:1+offset][:,::-1] # left part
        tmp[offset:-offset,-offset:] = image[:,-offset-1:-1][:,::-1] # right part
        tmp[0:offset,0:offset] = image[0:offset,0:offset][::-1,::-1] # upper left corner
        tmp[0:offset,-offset:] = image[0:offset,-offset:][::-1,::-1] # upper right corner
        tmp[-offset:,-offset:] = image[-offset:,-offset:][::-1,::-1] # lower rught corner
        tmp[-offset:,0:offset] = image[-offset:,0:offset][::-1,::-1] # lower left corner
    return pad_images

def pad_gpu(images, offset):
    """
        cupy.pad function
        constant values
    """
    h = images.shape[1]
    w = images.shape[2]
    pad_images = cp.zeros((images.shape[0], h+2*offset, w+2*offset), dtype = np.float32)
    for i in range(images.shape[0]):
        pad_images[i] = cp.pad(images[0], offset, mode = 'reflect')
    return pad_images

@jit(nopython=True) # 6 microseconds versus 45 microseconds without
def get_kernel(filter_size):
    """
        Get a gaussian kernel 
        Similar to opencv 
    """
    ksize = (3*filter_size)//2 * 2 + 1
    x = np.arange(ksize)
    g = np.power(x-((ksize-1)/2), 2)
    g = -(g/(2*(filter_size**2.0)))
    g = np.exp(g)
    ker = g/g.sum()    
    ker = np.atleast_2d(ker).T
    ker2D = ker.dot(ker.T)
    vmax = np.max(ker2D[0])
    kdims = ker2D.shape    
    ker2D = np.ravel(ker2D)
    nz = ker2D>=vmax
    zz = ker2D<vmax
    nzall = ker2D[nz]
    mk = np.mean(nzall)
    ker2D[nz] = ker2D[nz] - mk
    ker2D[zz] = 0
    ker2D = ker2D.reshape(kdims)
    ker2D = ker2D.astype(np.float32)
    return ker2D

# @jit(nopython=True)#, parallel=True)
def low_pass_filter_space(images, kernel, offset, h, w):
    """ Filter a 2D image

    Parameters : 
    -img_orig : ndarray, the original image.
    -kernel
    
    Return : 
    - filtered image

    """
    n = images.shape[0]
    pdims = (images.shape[1], images.shape[2])
    new_image = np.zeros((n, h, w), dtype = np.float32)
    for i in range(images.shape[0]):
        tmp1 = np.fft.rfftn(images[i], pdims) * np.fft.rfftn(kernel, pdims)
        tmp2 = np.fft.irfftn(tmp1)
        # tmp = scipy.signal.fftconvolve(images[i], kernel, mode = 'same')
        new_image[i] = tmp2[offset*2:,offset*2:]
    
    return new_image

# @cuda.jit
def low_pass_filter_space_gpu(images, new_images, kernel, offset):
    """ Filter a 2D image

    Parameters : 
    -img_orig : ndarray, the original image.
    -kernel
    
    Return : 
    - filtered image

    """
    pdims = (images.shape[1], images.shape[2])        
    for i in range(images.shape[0]):
        # stream = cp.cuda.stream.Stream()
        # with stream:
        tmp4 = cp.fft.irfft2(cp.fft.rfft2(images[i]) * cp.fft.rfft2(kernel, pdims))
        new_images[i] = tmp4[offset*2:,offset*2:]
        # stream.synchronize()
    
    return new_images

@jit(nopython=True)
def match_template(images, template, max_dev):
    """ 
        Perform matching template similar to opencv
        with TM_CCOEFF_NORMED
    """    
    res_all = np.zeros((images.shape[0], max_dev*2+1, max_dev*2+1))
    factor      = np.sum(np.power(template, 2.0))
    for i in range(images.shape[0]):        
        res = res_all[i]
        for j in range(max_dev*2+1):
            for k in range(max_dev*2+1):
                tmp = images[i,j:j+template.shape[0],k:k+template.shape[1]]
                res[j,k] = np.sum(tmp*template)/np.sqrt(np.sum(np.power(tmp,2.0))*factor)        
    return res_all

def match_template_gpu(images, template, max_dev):
    """ 
        Perform matching template similar to opencv
        with TM_CCOEFF_NORMED
    """    
    res_all     = cp.zeros((images.shape[0], max_dev*2+1, max_dev*2+1))
    factor      = cp.sum(cp.power(template, 2.0))
    for i in range(images.shape[0]):        
        res     = res_all[i]
        for j in range(max_dev*2+1):
            for k in range(max_dev*2+1):
                tmp         = images[i,j:j+template.shape[0],k:k+template.shape[1]]
                res[j,k]    = cp.sum(tmp*template)/cp.sqrt(cp.sum(cp.power(tmp,2.0))*factor)
        
    return res_all

def estimate_shifts(res_all, max_loc, max_dev):
    """
        Estimate shifts 
    """
    sh_x_n = np.zeros(max_loc.shape[0])
    sh_y_n = np.zeros(max_loc.shape[0])
    # if max is internal, check for subpixel shift using gaussian peak registration        
    tmp = np.logical_and(max_loc > 0, max_loc < 2*max_dev-1)
    index = tmp[:,0] * tmp[:,1]
    if np.any(index):
        ## from here x and y are reversed in naming convention         
        ish_x = max_loc[index,1]
        ish_y = max_loc[index,0]
        ires  = res_all[index]        
        idx1 = np.ravel_multi_index((np.arange(ish_x.shape[0]), ish_y-1, ish_x), dims = ires.shape)
        idx2 = np.ravel_multi_index((np.arange(ish_x.shape[0]), ish_y+1, ish_x), dims = ires.shape)
        idx3 = np.ravel_multi_index((np.arange(ish_x.shape[0]), ish_y, ish_x-1), dims = ires.shape)
        idx4 = np.ravel_multi_index((np.arange(ish_x.shape[0]), ish_y, ish_x+1), dims = ires.shape)
        idx5 = np.ravel_multi_index((np.arange(ish_x.shape[0]), ish_y, ish_x), dims = ires.shape)
        ires  = ires.flatten()
        log_xm1_y = np.log(ires[idx1])
        log_xp1_y = np.log(ires[idx2])
        log_x_ym1 = np.log(ires[idx3])
        log_x_yp1 = np.log(ires[idx4])
        four_log_xy = 4*np.log(ires[idx5])
        sh_x_n[index] = -(ish_x - max_dev + (log_xm1_y - log_xp1_y) / (2 * log_xm1_y - four_log_xy + 2 * log_xp1_y))
        sh_y_n[index] = -(ish_y - max_dev + (log_x_ym1 - log_x_yp1) / (2 * log_x_ym1 - four_log_xy + 2 * log_x_yp1))
    if np.any(~index):
        sh_x_n[~index] = -(max_loc[~index,1] - max_dev)
        sh_y_n[~index] = -(max_loc[~index,0] - max_dev)

    return sh_x_n, sh_y_n

def interpolate(new_ypos, new_xpos, image, xpos, ypos):
    f = interp2d(new_ypos, new_xpos, image, kind = 'linear')
    image = f(ypos, xpos)    
    return image

def warp_image(images, sh_x, sh_y):
    """
        Rigid shift + linear interpolation using scipy
        x is horizontal
        y is vertical
    """
    n, h, w = images.shape
    xpos = np.arange(h)    
    ypos = np.arange(w)

    for i in range(n):
        new_xpos = xpos + sh_x[i]
        new_ypos = ypos + sh_y[i]
        image = interpolate(new_ypos, new_xpos, images[i], xpos, ypos)
        images[i] = image.astype(np.float32)
    return images

def global_correct(images, template, dims, max_dev, filter_size):
    """ 
        Do a global correction of a set of images 
        matchTemplate and low pass filter space takes the longest time
        8 second for 200 frames
    """
    t1 = time()         

    # kernel for filtering
    kernel  = get_kernel(filter_size)
    ksize   = kernel.shape[0]
    offset  = (ksize-1)//2
    t2 = time()

    # preparing the template
    template_crop   = template.copy()
    template_crop   = template_crop[max_dev:-max_dev,max_dev:-max_dev]
    tdims           = template_crop.shape
    template_crop   = template_crop[np.newaxis]    
    template_padded = pad_array(template_crop, offset)
    t3 = time()

    # padding the images
    images = images.reshape(images.shape[0], dims[0], dims[1])
    images_padded   = pad_array(images, offset)
    t4 = time()

    # filtering images and template
    filtered_template = low_pass_filter_space(template_padded, kernel, offset, tdims[0], tdims[1])
    filtered_images = low_pass_filter_space(images_padded, kernel, offset, dims[0], dims[1])
    t5 = time()

    # match template
    filtered_template = np.squeeze(filtered_template, 0)
    res_all     = np.zeros((images.shape[0], max_dev*2+1, max_dev*2+1))
    match_template(filtered_images, filtered_template, max_dev, res_all)
    max_loc     = np.zeros((images.shape[0], 2), dtype = np.int)
    for i in range(images.shape[0]):
        res = res_all[i]
        max_loc[i] = np.array(np.unravel_index(np.argmax(res.flatten()), res.shape))
    t6 = time()

    # computing the shift    
    sh_x, sh_y = estimate_shifts(res_all, max_loc, max_dev)
    t7 = time()

    # shifting the image    
    images = warp_image(images, sh_x, sh_y)
    t8 = time()

    # flattening the images
    images = images.reshape(images.shape[0], np.prod(dims))
    t9 = time()

    print("kermel for filtering", t2 - t1)
    print("preparing the template", t3 - t2)
    print("padding the images", t4 - t3)
    print("filtering ", t5 - t4)
    print("match template", t6 - t5)
    print("computing the shift", t7 - t6)
    print("shifting the image", t8 - t7)
    print("reshaping ", t9 - t8)
    return images

###################################################################################
# MAIN LOOP
###################################################################################
def make_corrections(movie, start, end, template, dims, parameters): 
    """ 
    Do a global and a loc correction of a cluster of images
    """
    images = movie[start:end]
    # global correct
    images = global_correct(images, template, dims, parameters['max_deviation_rigid'], parameters['filter_size'])
    # # local correct
    # images = tile_and_correct(images, template, dims, parameters)
    movie[start:end] = images[:]
    return

###################################################################################
# TEMPLATE
###################################################################################
def get_template(movie, dims, start = 0, duration = 1):
    chunk = movie[start:start+duration]
    has_nan = np.any(np.isnan(chunk))
    if has_nan: 
        template     = np.nanmedian(chunk, axis = 0)
    else :
        template     = np.median(chunk, axis = 0)
    template = template.reshape(dims)
    return template

###################################################################################
# PREPROCESSING
###################################################################################
def get_video_info(files):
    """ In order to get the name, duration, start and end of each video
    
    Parameters:
    -files : the pathe where there is all the video (.avi)
    
    Returns:
    -videos : dictionnary of the videos from the miniscopes
    -video_info : DataFrame of informations about the video
    -dimension (h,w) of each frame """

    video_info  = pd.DataFrame(index = np.arange(len(files)), columns = ['file_name', 'start', 'end', 'duration'])
    videos      = dict.fromkeys(files) # dictionnary creation
    for f in files:
        num                                 = int(re.findall(r'\d+', f)[-1])
        video_info.loc[num,'file_name']     = f
        video                               = av.open(f)
        stream                              = next(s for s in video.streams if s.type == 'video') 
        video_info.loc[num, 'duration']     = stream.duration
        videos[f]                           = video

    video_info['start']     = video_info['duration'].cumsum()-video_info['duration']
    video_info['end']       = video_info['duration'].cumsum()
    video_info              = video_info.set_index(['file_name'], append = True)

    return video_info, videos, (stream.format.height, stream.format.width)

def get_hdf_file(videos, video_info, dims, save_original, **kwargs):
    """
    In order to convert the video into a HDF5 file.
    Parameters : 
    -videos : dictionnary of the videos from the miniscopes
    -video_info : DataFrame of informations about the video
    -dims : dimension (h,w) of each frame
    
    Returns :
    -file : HDF5 file"""
    hdf_mov     = os.path.split(video_info.index.get_level_values(1)[0])[0] + '/' + 'motion_corrected.hdf5'
    file        = hd.File(hdf_mov, "w")
    movie       = file.create_dataset('movie', shape = (video_info['duration'].sum(), np.prod(dims)), dtype = np.float32, chunks=True)
    if save_original:
        original = file.create_dataset('original', shape = (video_info['duration'].sum(), np.prod(dims)), dtype = np.float32, chunks=True)
    for v in tqdm(videos.keys()):
        offset  = int(video_info['start'].xs(v, level=1))
        stream  = next(s for s in videos[v].streams if s.type == 'video')        
        tmp     = np.zeros((video_info['duration'].xs(v, level=1).values[0], np.prod(dims)), dtype=np.float32)
        for i, packet in enumerate(videos[v].demux(stream)):
            frame           = packet.decode()[0].to_ndarray(format = 'bgr24')[:,:,0].astype(np.float32)
            tmp[i]          = frame.reshape(np.prod(dims))
            if i+1 == stream.duration : break                        
            
        movie[offset:offset+len(tmp),:] = tmp[:]
        if save_original:
            original[offset:offset+len(tmp),:] = tmp[:]
        del tmp
    if save_original:
        del original
    del movie 

    file.attrs['folder'] = os.path.split(video_info.index.get_level_values(1)[0])[0]
    file.attrs['filename'] = hdf_mov
    return file

###################################################################################
# MAIN FUNCTION
###################################################################################
def normcorre(fnames, procs, parameters):
    """
        see 
        Pnevmatikakis, E.A., and Giovannucci A. (2017). 
        NoRMCorre: An online algorithm for piecewise rigid motion correction of calcium imaging data. 
        Journal of Neuroscience Methods, 291:83-92
        or 
        CaiMan github
    """
    #################################################################################################
    # 1. Load every movies in only one file  or load the HDF if already present
    #################################################################################################
    video_info = None
    main_name, file_extension = os.path.splitext(fnames[0])    
    
    #fnames is the name of the file we will use 
    if file_extension == '.hdf5' : 
        hdf_mov = hd.File(fnames[0], 'r+')
        if 'original' in hdf_mov.keys():
            duration = hdf_mov['original'].attrs['duration']
            dims = tuple(hdf_mov['original'].attrs['dims'])

            if 'movie' not in hdf_mov.keys():                
                movie = hdf_mov.create_dataset('movie', shape = (duration, dims[0]*dims[1]), dtype = np.float32, chunks=True)
            
            size = hdf_mov['original'].chunks[0]
            starts = np.arange(0, duration, size)
            for i in starts:
                hdf_mov['movie'][i:i+size] = hdf_mov['original'].value[i:i+size]

        else:
            print("The key of the movie should be called 'original'")

    elif file_extension == '.avi':
        video_info, videos, dims = get_video_info(fnames)
        hdf_mov       = get_hdf_file(videos, video_info, dims, parameters['save_original'])
        duration    = video_info['duration'].sum() 

    else : 
        print ("Error : File extension not accepted") 
        sys.exit()

    #################################################################################################
    # 2. Estimate template from first n frame
    #################################################################################################
    template   = get_template(hdf_mov['movie'], dims, start = 0, duration = 500)
    
    #################################################################################################
    # 3. run motion correction / update template
    #################################################################################################    
    chunk_size      = parameters['block_size']- (parameters['block_size']%hdf_mov['movie'].chunks[0])
    chunk_starts    = np.arange(0,duration,chunk_size) 



     
    chunk_size  = hdf_mov['movie'].chunks[0] 
    chunk_starts_glob = np.arange(0, duration, chunk_size)
    nb_splits   = os.cpu_count() 

    block_size = parameters['block_size'] 
    coeff_euc = block_size//chunk_size # how many whole chunk there is in a block
    new_block = chunk_size*coeff_euc
    block_starts = np.arange(0,duration,new_block) 
       
    for i in range(parameters['nb_round']): # loop on the movie
        for start_block in tqdm(block_starts): # for each block
            chunk_starts_loc = np.arange(start_block,start_block+new_block,chunk_size)
            for start_chunk in chunk_starts_loc: # for each chunk                
                chunk_movie = hdf_mov['movie'][start_chunk:start_chunk+chunk_size]
                index = np.arange(chunk_movie.shape[0])
                splits_index = np.array_split(index, nb_splits)
                list_chunk_movie = [] #split of a chunk
                for idx in splits_index:
                    list_chunk_movie.append(chunk_movie[idx]) #each split of a chunk will be process in a different processor of the computer

                new_chunk = map_function(procs, nb_splits, list_chunk_movie, template, dims, parameters)
                new_chunk_arr = np.vstack(new_chunk)
                hdf_mov['movie'][start_chunk:start_chunk+chunk_size] = np.array(new_chunk_arr) #update of the chunk
                # if np.isinf(new_chunk_arr).sum(): Pdb().set_trace()

            template = get_template(hdf_mov['movie'], dims, start = start_block, duration = new_block) #update the template after each block 
    
    hdf_mov['movie'].attrs['dims'] = dims
    hdf_mov['movie'].attrs['duration'] = duration 

    return hdf_mov, video_info