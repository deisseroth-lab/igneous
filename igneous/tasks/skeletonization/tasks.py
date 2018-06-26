try:
    from StringIO import cStringIO as BytesIO
except ImportError:
    from io import BytesIO

import json
import pickle
import os
import re

import numpy as np
from tqdm import tqdm

from cloudvolume import CloudVolume
from cloudvolume.storage import Storage, SimpleStorage
from cloudvolume.lib import xyzrange, min2, max2, Vec, Bbox, mkdir
from taskqueue import RegisteredTask

from igneous import chunks, downsample, downsample_scales
from igneous import Mesher

from .definitions import Skeleton, Nodes
from .skeletonization import skeletonize
from .postprocess import crop_skeleton, merge_skeletons, trim_skeleton

def skeldir(cloudpath):
  with SimpleStorage(cloudpath) as storage:
    info = storage.get_json('info')

  skel_dir = 'skeletons/points'
  if 'skeletons' in info:
    skel_dir = info['skeletons']
  return skel_dir

class SkeletonTask(RegisteredTask):
  """
  Stage 1 of skeletonization.

  Convert chunks of segmentation into chunked skeletons and point clouds.
  They will be merged in the stage 2 task SkeletonMergeTask.
  """
  def __init__(self, cloudpath, shape, offset, mip, teasar_params=[10, 10]):
    super(SkeletonTask, self).__init__(cloudpath, shape, offset, mip, teasar_params)
    self.cloudpath = cloudpath
    self.bounds = Bbox(offset, Vec(*shape) + Vec(*offset))
    self.mip = mip
    self.teasar_params = teasar_params

  def execute(self):
    vol = CloudVolume(self.cloudpath, mip=self.mip, cache=True)
    bbox = Bbox.clamp(self.bounds, vol.bounds)

    image = vol[ bbox.to_slices() ]
    image = image[:,:,:,0]

    path = skeldir(self.cloudpath)
    path = os.path.join(self.cloudpath, path)

    with Storage(path) as stor:
      for segid, ptcloud in self.point_clouds(image, bbox):
        crop_bbox, skeleton = self.skeletonize(ptcloud, bbox)

        print(segid, ptcloud.shape, skeleton.empty())

        if skeleton.empty():
          continue

        stor.put_file(
          file_path="{}:skel:{}".format(segid, crop_bbox.to_filename()),
          content=pickle.dumps(skeleton),
          compress='gzip',
          content_type="application/python-pickle",
        )

  def skeletonize(self, point_cloud, bbox):
    skeleton = skeletonize(point_cloud, self.teasar_params)

    # Crop by 50px to avoid edge effects.
    crop_bbox = bbox.clone()
    crop_bbox.minpt += 50
    crop_bbox.maxpt -= 50

    if crop_bbox.volume() <= 0:
      return bbox, skeleton

    skeleton = crop_skeleton(skeleton, crop_bbox)
    return crop_bbox, skeleton

  # def point_clouds(self, image, bbox):
  #   """Mesh based point cloud finder. Not compatible with TEASAR unfortunately."""
  #   mesher = Mesher()
  #   mesher.mesh(image)
      
  #   for segid in mesher.ids():
  #     mesh = mesher.get_mesh(segid) # no simplificiation
  #     ptcloud = np.array(mesh['points'], dtype=np.float32)

  #     if ptcloud.size == 0:
  #       continue

  #     ptcloud = ptcloud.astype(np.int32) \
  #       .reshape( (ptcloud.shape[0] // 3, 3) )

  #     ptcloud[ :, 0 ] += bbox.minpt.x
  #     ptcloud[ :, 1 ] += bbox.minpt.y
  #     ptcloud[ :, 2 ] += bbox.minpt.z

  #     yield segid, ptcloud

  def point_clouds(self, image, bbox):
    uniques = np.unique(image)
    
    for segid in uniques:
      if segid == 0:
        continue 

      # type = int32 b/c coords can be less than zero after bbox adjustment
      # and int32 saves 2x over int64 default
      coords = np.argwhere( image == segid ).astype(np.int32)
      
      if coords.size == 0:
        continue

      coords[ :, 0 ] += bbox.minpt.x
      coords[ :, 1 ] += bbox.minpt.y
      coords[ :, 2 ] += bbox.minpt.z
      yield segid, coords

class SkeletonMergeTask(RegisteredTask):
  """
  Stage 2 of skeletonization.

  Combine point cloud chunks into a single unified point cloud.

  If we parallelize using prefixes single digit prefixes ['0','1',..'9'] all meshes will
  be correctly processed. But if we do ['10','11',..'99'] meshes from [0,9] won't get
  processed and need to be handle specifically by creating tasks that will process
  a single mesh ['0:','1:',..'9:']
  """
  def __init__(self, cloudpath, prefix):
    super(SkeletonMergeTask, self).__init__(cloudpath, prefix)
    self.cloudpath = cloudpath
    self.prefix = prefix

  def execute(self):
    self.skeldir = skeldir(self.cloudpath)

    with Storage(self.cloudpath) as storage:
      self.agglomerate(storage)

  def _get_filenames_subset(self, storage):
    prefix = '{}/{}'.format(self.skeldir, self.prefix)
    pointclouds = defaultdict(list)
    skeletons = defaultdict(list)

    for filename in storage.list_files(prefix=prefix):
      filename = os.path.basename(filename)
      # `match` implies the beginning (^). `search` matches whole string
      matches = re.search(r'(\d+):(ptcloud|skel):', filename)

      if not matches:
        continue

      segid, filetype = matches.groups()
      segid = int(segid)

      if filetype == 'ptcloud':
        pointclouds[segid].append(filename)
      elif filetype == 'skel':
        skeletons[segid].append(filename)
      else:
        raise ValueError("{} is not a known file type. Accepted: ptcloud, skel".format(filetype))

    return (pointclouds, skeletons)

  def agglomerate(self, stor):
    ptclouds, skels = self._get_filenames_subset(stor)

    vol = CloudVolume(self.cloudpath)

    for segid, frags in skels.items():
      ptcloud = self.fuse_point_cloud(ptclouds[segid])
      skeleton = self.fuse_skeletons(frags, stor)
      skeleton = trim_skeleton(skeleton, ptcloud)

      stor.put_file(
        file_path="{}.pkl".format(segid),
        content=pickle.dumps(skeleton),
        compress='gzip',
        content_type="application/python-pickle",
      )

      vol.skeleton.save(segid, skeleton.vertices, skeleton.edges)

    stor.wait()

    for segid, frags in skels.items():
      stor.delete_files(frags)

  def fuse_skeletons(filenames, storage):
    if len(filenames) == 0:
      return Skeleton()
    
    skldl = storage.get_files(frags)
    skeletons = { item['filename'] : pickle.loads(item['content']) for item in skldl }

    if len(skeletons) == 1:
      return skeletons[filenames[0]]

    file_pairs = self.find_paired_skeletons(frags)

    for fname1, fname2 in file_pairs:
      skel1, skel2 = skeletons[fname1], skeletons[fname2]
      skel1, skel2 = merge_skeletons(skel1, skel2)
      skeletons[fname1] = skel1
      skeletons[fname2] = skel2

    skeletons = list(skeletons.values())

    fusing = skeletons[0]
    offset = 0
    for skel in skeletons[1:]:
      if skel.edges.shape[0] == 0:                                                                                                                                                                                                                                                                                                                                                            
        continue

      skel.edges = skel.edges.astype(np.uint32)
      skel.edges += offset
      offset += skel.nodes.shape[0]

      fusing.nodes = np.concatenate((fusing.nodes, skel.nodes), axis=0)
      fusing.edges = np.concatenate((fusing.edges, skel.edges), axis=0)
      fusing.radii = np.concatenate((fusing.radii, skel.radii), axis=0)

    return consolidate_skeleton(fusing)

  def find_paired_skeletons(self, filenames):
    pairs = []

    for i in range(len(filenames) - 1):
      adj1 = Bbox.from_filename(filenames[i])
      for j in range(i + 1, len(filenames)):
        adj2 = Bbox.from_filename(filenames[j])

        # We're testing for overlap, tasks
        # are created with 50% overlap
        if Bbox.intersects(adj1, adj2):
          pairs.append(
            (filenames[i], filenames[j])
          )

    return pairs

  def fuse_point_cloud(self, ptcloudchunks):
    points = [ f['content'] for f in storage.get_files(ptcloudchunks) ]

    for i in range(len(points)):
      buf = points[i]
      shape = (len(buf) // 3, 3) # 2d list of xyz
      points[i] = np.frombuffer(buf, dtype=np.int32).reshape(shape, order='C')

    points = np.concatenate(*points, axis=0)
    points.sort(axis=0) # sorts x column, but not y unfortunately
    points = np.unique(points)

    storage.put_file(
      file_path='{}/{}.ptc'.format(self.skeldir, segid),
      content=points.tobytes('C'),
      compress='gzip',
    ).wait()

    storage.delete_files(frags)

    # bboxes = [ Bbox.from_filename(fname) for fname in ptcloudchunks ]
    # bbx = Bbox.expand(*bboxes)

    return points





