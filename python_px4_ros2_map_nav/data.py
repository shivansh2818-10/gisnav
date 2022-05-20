"""Module containing data structures to protect atomicity of related information"""
from __future__ import annotations  # Python version 3.7+

import cv2
import numpy as np
import os

from xml.etree import ElementTree
from typing import Optional, Union, get_args
from collections import namedtuple
from dataclasses import dataclass, field
from multiprocessing.pool import AsyncResult

from python_px4_ros2_map_nav.assertions import assert_type, assert_ndim, assert_shape

BBox = namedtuple('BBox', 'left bottom right top')  # Convention: https://wiki.openstreetmap.org/wiki/Bounding_Box
LatLon = namedtuple('LatLon', 'lat lon')
LatLonAlt = namedtuple('LatLonAlt', 'lat lon alt')
Dim = namedtuple('Dim', 'height width')
RPY = namedtuple('RPY', 'roll pitch yaw')
TimePair = namedtuple('TimePair', 'local foreign')


# noinspection PyClassHasNoInit
@dataclass(frozen=True)
class _Image:
    """Parent dataclass for image holders

    Should not be instantiated directly.
    """
    image: np.ndarray


# noinspection PyClassHasNoInit
@dataclass(frozen=True)
class ImageData(_Image):
    """Keeps image frame related data in one place and protects it from corruption."""
    #image: np.ndarray
    frame_id: str
    timestamp: int
    k: np.ndarray
    img_dim: Dim  # TODO: redundant, or make this declared, k overrides!


# noinspection PyClassHasNoInit
@dataclass(frozen=True)
class MapData(_Image):
    """Keeps map frame related data in one place and protects it from corruption."""
    #image: np.ndarray
    center: Union[LatLon, LatLonAlt]
    radius: Union[int, float]
    bbox: BBox
    dim: Dim  # this is the original map dimension/resolution with padding, .image shape


# noinspection PyClassHasNoInit
@dataclass(frozen=True)
class ContextualMapData(_Image):
    """Contains the rotated and cropped map image for _pose estimation"""
    #image: np.ndarray  # This is the map_cropped image which is same size as the camera frames
    rotation: Union[float, int]
    img_dim: Dim  # TODO: unnecessary, just get image.shape?
    map_data: MapData   # This is the original larger (square) map with padding


# TODO: enforce types for ImagePair (img cannot be MapData, can happen if _pose.__matmul__ is called in the wrong order! E.g. inside _estimate_map_pose
# noinspection PyClassHasNoInit
@dataclass(frozen=True)
class ImagePair:
    """Atomic image pair to represent a matched pair of images"""
    img: ImageData
    ref: Union[ImageData, ContextualMapData]  # TODO: _Image? Or exclude MapData?

    def mapful(self) -> bool:
        """Returns True if this image pair is for a map match

        :return: True for map match, False for visual odometry match
        """
        return isinstance(self.ref, ContextualMapData)  # TODO: get_args(Union[ContextualMapData, MapData]) ?


# noinspection PyClassHasNoInit
@dataclass(frozen=True)
class AsyncQuery:
    """Atomic pair that stores a :py:class:`multiprocessing.pool.AsyncResult` instance along with its input data

    The intention is to keep the result of the query in the same place along with the inputs so that they can be
    easily reunited again in the callback function. The :meth:`python_px4_ros2_map_nav.matchers.matcher.Matcher.worker`
    interface expects an image_pair and an input_data context as arguments (along with a guess which is not stored
    since it is no longer needed after the _pose estimation).
    """
    result: AsyncResult
    #query: Union[ImagePair, ]  # TODO: what is used for WMS?
    image_pair: ImagePair  # TODO: what is used for WMS?
    input_data: InputData


# noinspection PyClassHasNoInit
@dataclass(frozen=True)
class Pose:
    """Represents camera _pose (rotation and translation) along with camera intrinsics"""
    image_pair: ImagePair
    r: np.ndarray
    t: np.ndarray
    e: np.ndarray = field(init=False)
    h: np.ndarray = field(init=False)
    inv_h: np.ndarray = field(init=False)
    fx: float = field(init=False)
    fy: float = field(init=False)
    cx: float = field(init=False)  # TODO: int?
    cy: float = field(init=False)  # TODO: int?
    camera_position: np.ndarray = field(init=False)
    camera_center: np.ndarray = field(init=False)
    camera_position_difference: np.ndarray = field(init=False)

    def __post_init__(self):
        """Set computed fields after initialization."""
        # Data class is frozen so need to use object.__setattr__ to assign values
        object.__setattr__(self, 'e', np.hstack((self.r, self.t)))  # -self.r.T @ self.t
        object.__setattr__(self, 'h', self.image_pair.img.k @ np.delete(self.e, 2, 1))  # Remove z-column, making the matrix square
        object.__setattr__(self, 'inv_h', np.linalg.inv(self.h))
        object.__setattr__(self, 'fx', self.image_pair.img.k[0][0])
        object.__setattr__(self, 'fy', self.image_pair.img.k[1][1])
        object.__setattr__(self, 'cx', self.image_pair.img.k[0][2])
        object.__setattr__(self, 'cy', self.image_pair.img.k[1][2])
        object.__setattr__(self, 'camera_position', -self.r.T @ self.t)
        object.__setattr__(self, 'camera_center', np.array((self.cx, self.cy, -self.fx)).reshape((3, 1)))  # TODO: assumes fx == fy
        object.__setattr__(self, 'camera_position_difference', self.camera_position - self.camera_center)

    def __matmul__(self, pose: Pose) -> Pose:  # Python version 3.5+
        """Matrix multiplication operator for convenience

        Returns a new _pose by combining two camera relative poses:

        pose1 @ pose2 =: Pose(pose1.r @ pose2.r, pose1.t + pose1.r @ pose2.t)

        A new 'synthetic' image pair is created by combining the two others.
        """
        assert (self.image_pair.img.k == pose.image_pair.img.k).all(), 'Camera intrinsic matrices are not equal'  # TODO: validation, not assertion
        return Pose(
                image_pair=ImagePair(img=self.image_pair.img, ref=pose.image_pair.ref),
                r=self.r @ pose.r,
                t=self.t + self.r @ (pose.t + pose.camera_center) # TODO: need to fix sign somehow? Would think minus sign is needed here?
        )


# noinspection PyClassHasNoInit
@dataclass(frozen=True)
class InputData:
    """InputData of vehicle state and other variables needed for postprocessing both map and visual odometry matches.

    :param vo_fix: - The WGS84-fixed FixedCamera for the VO reference frame, or None if not available
    :return:
    """
    vo_fix: Optional[FixedCamera]  # None if successful map match has not yet happened

    def __post_init__(self):
        """Validate the data structure"""
        # TODO: Enforce types
        pass


# noinspection PyClassHasNoInit
@dataclass
class FOV:
    """Camera field of view related attributes"""
    fov_pix: np.ndarray
    fov: Optional[np.ndarray]  # TODO: rename fov_wgs84? Can be None if can't be projected to WGS84?
    c: np.ndarray
    c_pix: np.ndarray
    pix_to_wgs84: Optional[np.ndarray]  # TODO: None if cannot be projected to wgs84?


# noinspection PyClassHasNoInit
@dataclass
class FixedCamera:
    """WGS84-fixed camera attributes

    Colletcts field of view and map_pose under a single structure that is intended to be stored in input data context as
    visual odometry fix reference. Includes the needed map_pose and pix_to_wgs84 transformation for the vo fix.
    """
    fov: FOV
    map_pose: Pose


# noinspection PyClassHasNoInit
@dataclass
class OutputData:
    # TODO: add extrinsic matrix / _pose, pix_to_wgs84 transformation?
    # TODO: freeze this data structure to reduce unintentional re-assignment?
    """Algorithm output passed onto publish method.

    :param input: The input data used for the match
    :param _pose: Estimated _pose for the image frame vs. the map frame
    :param fixed_camera: Camera that is fixed to wgs84 coordinates (map_pose and field of view)
    :param position: Vehicle position in WGS84 (elevation or z coordinate in meters above mean sea level)
    :param terrain_altitude: Vehicle altitude in meters from ground (assumed starting altitude)
    :param attitude: Camera attitude quaternion
    :param sd: Standard deviation of position estimate
    :return:
    """
    input: InputData
    _pose: Pose  # should not be accessed directly except e.g. for debug visualization, use map_pose instead
    fixed_camera: FixedCamera
    position: LatLonAlt
    terrain_altitude: float
    attitude: np.ndarray
    sd: np.ndarray  # TODO This should be part of Position? Keep future position dataclass mutable so this can be assigned while outputdata itself is immutable

    # Target structure:
    # input
    # vehicle (position, attitude, terrain_altitude, sd)
    # camera (map_pose, fov)  +  camera attitude which is actually what we have now

    def __post_init__(self):
        """Validate the data structure"""
        # TODO: Enforce types
        pass


# noinspection PyClassHasNoInit
@dataclass(frozen=True)
class PackageData:
    """Stores data parsed from package.xml (not comprehensive)"""
    package_name: str
    version: str
    description: str
    author: str
    author_email: str
    maintainer: str
    maintainer_email: str
    license_name: str


def parse_package_data(package_file: str) -> PackageData:
    """Parses package.xml in current folder

    :param package_file: Absolute path to package.xml file
    :return: Parsed package data
    :raise FileNotFoundError: If package.xml file is not found
    """
    if os.path.isfile(package_file):
        tree = ElementTree.parse(package_file)
        root = tree.getroot()
        package_data = PackageData(
            package_name=root.find('name').text,
            version=root.find('version').text,
            description=root.find('description').text,
            author=root.find('author').text,
            author_email=root.find('author').attrib.get('email', ''),
            maintainer=root.find('maintainer').text,
            maintainer_email=root.find('maintainer').attrib.get('email', ''),
            license_name=root.find('license').text
        )
        return package_data
    else:
        raise FileNotFoundError(f'Could not find package file at {package_file}.')
