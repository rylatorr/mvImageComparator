#!/usr/bin/python3
'''
mvImageComparitor
Author: Ryan LaTorre <ryan@latorre.ca>
Description: Fetch MV snapshots and use image analysis to identify cameras that need attention
Learn more at http://github.com/rylatorr/mvImageComparitor
'''

import sys
import argparse
import os
import shutil
import logging
import configparser
import meraki
import json
import requests
import urllib.request
from datetime import datetime, timedelta
import time
import cv2

logger = logging.getLogger("filelogger")

def printHelp():
    lines = READ_ME.split('\n')
    for line in lines:
        print('# {0}'.format(line))

def setupSession():
    configDict = readConfigVars()

    debugging = False
    debugging = configDict['general']['debugging']
    logger.setLevel(logging.DEBUG if debugging else logging.WARNING)
    handler = logging.FileHandler(os.path.join(os.path.dirname(__file__), 'mvImageComparator.log'))
    handler.setLevel(logging.DEBUG if debugging else logging.WARNING)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.debug(f"setupSession: Logging set to debug")

    # Instantiate a Meraki dashboard API session
    dashboard = meraki.DashboardAPI(
        configDict['meraki']['apikey'],
        output_log=False,
        print_console=False
    )
    return configDict, dashboard

def configToDict(config):
    # Converts a ConfigParser object into a dictionary.
    # The resulting dictionary has sections as keys which point to a dict of the
    # sections options as key => value pairs.
    configDict = {}
    for section in config.sections():
        configDict[section] = {}
        for key, val in config.items(section):
            configDict[section][key] = val
    return configDict

def readConfigVars():
    config_file = os.path.join(os.path.dirname(__file__), 'config/config.ini')
    config = configparser.ConfigParser()
    try:
        config.read(config_file)
        configDict = configToDict(config)
        logger.debug(f"readConfigVars: configDict: {configDict}")
    except:
        logger.error("readConfigVars: Missing config items or file!")
        sys.exit(2)

    logger.debug("Finished reading config vars.")
    return configDict

# Get Webex bot's rooms
def getWebexBotRooms(session, headers):
    response = session.get('https://webexapis.com/v1/rooms', headers=headers)
    return response.json()['items']

# Get room ID for desired space
def getWebexRoomId(session, headers, webexNotificationRoomName):
    rooms = getWebexBotRooms(session, headers)
    for room in rooms:
        if room["title"].startswith(webexNotificationRoomName):
            webexRoomId = room["id"]
            return webexRoomId
    return False

# Send a message in Webex.
def postWebexMessage(session, headers, payload, message):
    payload['markdown'] = message
    session.post('https://webexapis.com/v1/messages/',
                 headers=headers,
                 data=json.dumps(payload))

def postReport(configDict, dashboard, suspectCams):
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {configDict['webex']['webexbottoken']}"
    }
    session = requests.Session()
    webexRoomId = getWebexRoomId(session, headers, configDict['webex']['roomname'])
    payload = {'roomId': webexRoomId}
    # message header row
    message = f"{configDict['webex']['msgprefix']}:  {datetime.now().strftime('%a %b %d, %I:%M %p')}. "
    for cam in suspectCams:
        timestamp = (datetime.now() - timedelta(seconds=15) - timedelta(hours=3)).isoformat()
        deviceName = dashboard.devices.getDevice(cam)['name']
        videoLinkResp = dashboard.camera.getDeviceCameraVideoLink(cam, timestamp=timestamp)
        videoLink = videoLinkResp['url']
        message += f"\n{cam} : [{deviceName}]({videoLink})"
    if len(suspectCams) > 0:
        postWebexMessage(session, headers, payload, message)
    return

def getOrgId(configDict, dashboard):
    # Get list of organizations to which API key has access
    orgName = configDict['meraki']['orgname']
    logger.debug(f"getOrgId: desired org name is: {orgName}")
    organizations = dashboard.organizations.getOrganizations()
    #logger.debug(f"getOrgId: allorganizations are: {organizations}")

    # Iterate through list of orgs to get the one I want
    for org in organizations:
        org_index = next((index for (index, d) in enumerate(organizations) if d['name'].lower() == orgName.lower()), None)
        if org_index is None:
            logger.error(f"{orgName} not found in list of orgs available to supplied API key")
            exit()
        else:
            orgId = organizations[org_index]['id']
    return(orgId)

def getNewReferenceSnapshots(configDict, dashboard, newReferenceDevices):
    newReferenceSnapshots = {}
    for mvSerial in newReferenceDevices:
        # Generate a snapshot
        snapshotLinkResp = dashboard.camera.generateDeviceCameraSnapshot(mvSerial)
        snapshotLink = snapshotLinkResp['url']
        logger.debug(f"getNewReferenceSnapshots: Link to snap is: {snapshotLink}")
        newReferenceSnapshots[mvSerial] = snapshotLink

        # Remove the device tag used to trigger new reference image retrieval
        currentTags = dashboard.devices.getDevice(mvSerial).get('tags')
        logger.debug(f"getNewReferenceSnapshots: currentTags list is: {currentTags}")
        currentTags.remove(configDict['meraki']['newreferencetag'])
        dashboard.devices.updateDevice(mvSerial, tags=currentTags)

    for key, value in newReferenceSnapshots.items():
        # Download snapshot to referenceImages directory
        imageName = 'referenceImages/' + key + '.jpg'
        logger.debug(f"getNewReferenceSnapshots: downloading new reference snap: {imageName} from {value}")
        urllib.request.urlretrieve(value, imageName)
    return

def getTestSnapshots(configDict, dashboard, compareList):
    logger.debug(f"getTestSnapshots: collecting images to compare with those on file")
    testSnapshots = {}
    for mvSerial in compareList:
        # Generate a snapshot
        snapshotLinkResp = dashboard.camera.generateDeviceCameraSnapshot(mvSerial)
        snapshotLink = snapshotLinkResp['url']
        logger.debug(f"getTestSnapshots: Link to snapshot is: {snapshotLink}")
        testSnapshots[mvSerial] = snapshotLink

    for key, value in testSnapshots.items():
        # Download snapshot to testImages directory
        imageName = 'testImages/' + key + '.jpg'
        urllib.request.urlretrieve(value, imageName)
    return

def imageSIFTCompare(configDict, mvSerial):
    logger.debug(f"starting imageSIFTCompare")
    # Read images into CV
    original = cv2.imread('./referenceImages/' + mvSerial + '.jpg')
    image_to_compare = cv2.imread('./testImages/' + mvSerial + '.jpg')

    # 1) Check if 2 images are equals
    if original.shape == image_to_compare.shape:
        logger.debug(f"imageSIFTCompare: The images have same size and channels")
        difference = cv2.subtract(original, image_to_compare)
        b, g, r = cv2.split(difference)

        if cv2.countNonZero(b) == 0 and cv2.countNonZero(g) == 0 and cv2.countNonZero(r) == 0:
            logger.debug(
                f"imageSIFTCompare: The images are completely Equal, was this camera {mvSerial} newly added to monitoring?")
        else:
            logger.debug(f"imageSIFTCompare: The images are NOT equal, continuing comparison")

    # convert images to grayscale to improve performance
    original = cv2.cvtColor(original, cv2.COLOR_BGR2GRAY)
    image_to_compare = cv2.cvtColor(image_to_compare, cv2.COLOR_BGR2GRAY)

    # Opt shrink each image to a max dimension (I've chosen 540x960 as 50% of 1080p)
    logger.debug(f"Original original Dimensions : {original.shape}")
    logger.debug(f"Test Subject original Dimensions : {image_to_compare.shape}")
    maxwidth, maxheight = 960, 540
    f1 = maxwidth / original.shape[1]
    f2 = maxheight / original.shape[0]
    f = min(f1, f2)  # resizing factor
    dim = (int(original.shape[1] * f), int(original.shape[0] * f))
    original = cv2.resize(original, dim, interpolation=cv2.INTER_AREA)

    f1 = maxwidth / image_to_compare.shape[1]
    f2 = maxheight / image_to_compare.shape[0]
    f = min(f1, f2)  # resizing factor
    dim = (int(image_to_compare.shape[1] * f), int(image_to_compare.shape[0] * f))
    image_to_compare = cv2.resize(image_to_compare, dim, interpolation=cv2.INTER_AREA)

    logger.debug(f"{mvSerial} Original new Dimensions : {original.shape}")
    logger.debug(f"{mvSerial} Test Subject new Dimensions : {image_to_compare.shape}")

    # 2) Check for similarities between the 2 images
    # I'm having a lot of difficulty getting SIFT to find and match good descriptors,
    # SIFT parameter defaults are sigma 1.6, contrast 0.04, edge 10
    # sift = cv2.SIFT_create(sigma=1.2, contrastThreshold=0.01, edgeThreshold=20)
    sift = cv2.SIFT_create()
    kp_1, desc_1 = sift.detectAndCompute(original, None)
    kp_2, desc_2 = sift.detectAndCompute(image_to_compare, None)

    index_params = dict(algorithm=0, trees=5)
    search_params = dict()
    flann = cv2.FlannBasedMatcher(index_params, search_params)
    if desc_1 is None:
        logger.debug(f"{mvSerial} Descriptors empty on reference image")
        return False
    if desc_2 is None:
        logger.debug(f"{mvSerial} Descriptors empty on test image")
        return False

    matches = flann.knnMatch(desc_1, desc_2, k=2)
    good_points = []
    # ratio = 0.6
    # ratio = 0.4
    ratio = float(configDict['general']['siftratio'])

    for m, n in matches:
        if m.distance < ratio * n.distance:
            good_points.append(m)
    logger.debug(f"{mvSerial} Number of good points: {str(len(good_points))}")
    result = cv2.drawMatches(original, kp_1, image_to_compare, kp_2, good_points, None)

    # Debug CV options: can save or show images on screen. Deciding to save to disk by default.
    #cv2.imshow("result", result)
    cv2.imwrite('./testImages/' + mvSerial + '-result.jpg', result)
    # cv2.imshow("Original", original)
    # cv2.imshow("Test Subject", image_to_compare)
    #cv2.waitKey(0)
    #cv2.destroyAllWindows()

    # If image differs, add to list for summary report
    if len(good_points) < int(configDict['general']['siftmatches']):
        return False
    else:
        return True

def compareScenes(configDict, compareList):
    logger.debug(f"compareScenes: starting")
    sceneMatches = False
    suspectCams = []
    for mvSerial in compareList:
        # If no reference image exists, consider the current snapshot the new reference image
        # yes, will always result in a perfect match the first time and could result in false
        # matches later but those can be corrected by setting the new reference image tag on Dash.
        refFile = './referenceImages/' + mvSerial + '.jpg'
        testFile = './testImages/' + mvSerial + '.jpg'
        if os.path.isfile(refFile) is False:
            shutil.copyfile(testFile, refFile)

        sceneMatches = imageSIFTCompare(configDict, mvSerial)
        # If image differs, add to list for summary report
        if sceneMatches is False:
            logger.debug(f"compareScenes: Found Different Image: {mvSerial} differs by {configDict['general']['siftmatches']} points")
            suspectCams.append(mvSerial)
        else:
            logger.debug(f"compareScenes: Image was similar enough")
    return suspectCams

def getNetworksToMonitor(configDict, dashboard):
    logger.debug(f"getNetworksToMonitor: collecting networks to monitor")
    networksToMonitor = []
    # Get orgID
    orgId = getOrgId(configDict, dashboard)
    # Find all networks
    #networks = dashboard.organizations.getOrganizationNetworks(configDict['meraki']['orgid'])
    networks = dashboard.organizations.getOrganizationNetworks(orgId)

    # Iterate through all networks
    for network in networks:
        # Skip if network does not have the tag defined in config file
        networkTag = configDict['meraki']['networktag']
        if network['tags'] is None or networkTag not in network['tags']:
            continue
        else:
            networksToMonitor.append(network['id'])
    logger.debug(f"getNetworksToMonitor: network IDs to monitor: {networksToMonitor}")
    return networksToMonitor

def getCameraList(configDict, dashboard, networksToMonitor):
    compareDevices = []
    newReferenceDevices = []
    devices = []

    for networkid in networksToMonitor:
        devices = dashboard.networks.getNetworkDevices(networkid)

        # Add camera to lists
        for device in devices:
            if "MV" in device['model']:
                if configDict['meraki']['comparetag'] in device['tags']:
                    compareDevices.append(device['serial'])
                if configDict['meraki']['newreferencetag'] in device['tags']:
                    newReferenceDevices.append(device['serial'])

    logger.debug(f"getCameraList: device serial numbers IDs to compare: {compareDevices}")
    logger.debug(f"getCameraList: device serial numbers IDs to replace reference images: {newReferenceDevices}")
    return compareDevices, newReferenceDevices

def main(args):
    # Get command line arguments
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('-t', '--test', help='Test comparison for serial # using cached images')
    parsed_args = arg_parser.parse_args()

    if parsed_args.test:
        testSerial = parsed_args.test
        configDict, dashboard = setupSession()
        imageSIFTCompare(configDict, testSerial)
        sys.exit()

    configDict, dashboard = setupSession()
    logger.info(f"Starting mvImageComparator")
    networksToMonitor = getNetworksToMonitor(configDict, dashboard)
    compareDevices, newReferenceDevices = getCameraList(configDict, dashboard, networksToMonitor)
    getNewReferenceSnapshots(configDict, dashboard, newReferenceDevices)
    getTestSnapshots(configDict, dashboard, compareDevices)
    suspectCams = compareScenes(configDict, compareDevices)
    postReport(configDict, dashboard, suspectCams)
    return

if __name__ == "__main__":
    main(sys.argv[1:])
