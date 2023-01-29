#!/usr/bin/python3
#-*- coding: utf-8 -*-
#
#
# Battery control system
# ====================
#
# This scripts fetches daily electricity prices from Tibber and controls charching/discharging of a LUNA2000 battery through HomeAssistant. 
# All commands vs. HomeAssistant is made over REST.
# 
# 
##############################################################################################################################################
from re import I, L
from socket import TIPC_MEDIUM_IMPORTANCE
import time,datetime
import argparse
from requests import post,get
import json
from scipy.signal import find_peaks
import math

# Local data and secrets

# Moved to import file guarded by .gitignore
#
#HA_URL = "http://xxx.xxx.zzz.yyy:8123"
#HA_TOKEN = "verylongstring......."
#TIBBER_TOKEN = "notsolongstring......"
import privatetokens

TIBBER_URL ="https://api.tibber.com/v1-beta/gql"

# Default values, can be changed by command line options

LOGFILE="./battery.log"
WAIT = 10                   # seconds between loops
LOGLEVEL='ERROR'
TEST = False
PRICECONTROL = False        # Will include setting of pricelevel in HA if set

# Constants

NETTRANSFERCOST=0.70        # Cost for network transfer to be used to calculate price for charge of battery.
INVERTERLOSS=0.05
CYCLELENGTH = 3             # no of hours for a complete charging/discharging hours
NOCHARGEHOUR = 8            # TOU mode (used for charging) needs one discharge segment. This hour will be blocked for charging, i.e no 'L' setting this hour
CHARGINGPOWER = 2.5         # Charging and discharging power (kW)

#########################################################
#
# Class holding data to communicate with Home Assistant
#
#########################################################   

class homeAssistant:

    def __init__(self,url,token,logger):
    
        #
        #   create object holding server
        #

        self.headers = {
            "Authorization": "Bearer " + token,
            "content-type": "application/json",
        }
        self.url = url
        self.logger = logger

###################################################################
#
# Class with methods setting and getting Home Assistant entity data
#
###################################################################   

class haEntity():
    
    def __init__(self,ha,id):

        self.url=ha.url
        self.headers = ha.headers
        self.id = id
        self.logger = ha.logger

        
    def getState(self):
        response = get(self.url + "/api/states/" + self.id, headers=self.headers)
        return json.loads(response.text)['state']

    def setState(self,state,attributes={}):
        logger = self.logger
        payload = {
            "state" : state
        }
        # Add attributes to entity if provided
        if attributes:
            payload["attributes"] = attributes

        response = post(self.url + "/api/states/" + self.id, headers=self.headers, json=payload )
        if not response.ok:
            logger.error('Failed to send request to homeassistant: ' + str(response.status_code) +
                ' - ' + response.reason + ', url: ' + response.url + ', req: ' + str(payload) + ', response_req: ' + str(response.request.body))

        return response.ok

    def turnOn(self):
        payload = {
            "entity_id" : self.id
        }
        response = post(self.url + "/api/services/switch/turn_on", headers=self.headers ,json=payload)
        return response.ok

    def turnOff(self):
        payload = {
            "entity_id" : self.id
        }
        response = post(self.url + "/api/services/switch/turn_off", headers=self.headers ,json=payload)
        return response.ok

    
###########################################################################################################
#
# Get command line parameters
# 
# For options use -h
#
# All options impact a number of global variables. Default values as defined in the beginning of this file
#
###########################################################################################################
def get_cmd_line_parameters():

    global LOGFILE,LOGLEVEL,TEST,PRICECONTROL

    parser = argparse.ArgumentParser( description='Battery charging control daemon' )
    parser.add_argument("-v", "--loglevel", help="Log level. DEBUG, WARNING, INFO, ERROR or CRITICAL. Default ERROR.",default=LOGLEVEL)
    parser.add_argument("-l", "--logfile", help="Log file. Default " + LOGFILE, default=LOGFILE)
    parser.add_argument("-t", "--test", help="Test mode. Will test HA and TIBBER interface. No loop and nothing set in HA. Logging set to INFO and name set to batterytest.log", action="store_true")
    parser.add_argument("-p", "--pricecontrol", help="Price control. Will control setting of entity input_select.heating_level.", action="store_true")

                        
    args = parser.parse_args()
    LOGFILE = args.logfile
    LOGLEVEL=args.loglevel
    if args.test : 
        LOGFILE = "batterytest.log"
        
        #LOGLEVEL= "DEBUG"
        TEST = True
    if args.pricecontrol :
        PRICECONTROL = True
        
####################################################
#
# Function to setup a logger for this application
#
####################################################

def logger(name,level):
    import logging, logging.handlers  
    # 
    # Set up a file logger
    #
    handler = logging.handlers.RotatingFileHandler(name,maxBytes=200000,backupCount=10)     # create a file handler
    handler.setLevel(level)   
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')   # define logging format
    handler.setFormatter(formatter)
    log = logging.getLogger(__name__)   
    log.addHandler(handler)                                                              # add the handler to the logger
    #
    # Modify default logger with appropriate level
    #
    logging.basicConfig(level=level)
    return log

#
# Function to fetch elecitricy prices from Tibber broker for today and tomorrow (if available)
#

def getPrices(logger):
    authorization = {"Authorization": "Bearer" + privatetokens.TIBBER_TOKEN , "Content-Type":"application/json"}
    gql = '{ "query": "{viewer {homes {currentSubscription {priceInfo {current {total energy tax startsAt} today {total energy tax startsAt} tomorrow { total energy tax startsAt }} }}}}"} '
    try:
        response = post(TIBBER_URL, data=gql, headers=authorization)
    except Exception as err:
        logger.error("Error connecting to Tibber")
        logger.error(err)
        quit()
    return response
#
#
#  
# This function builds a charging vector based on today's prices
#
#
#


def buildOptimizedChargeCntrlVector(data,logger):

    if (TEST) : data = testdata(data)           # Supports swap with testdata in test mode
    vectorsegment = buildChargeCntrlVector(data,logger)
    if len(vectorsegment) == 0 : vectorsegment = ['0']*24
    logger.info('')
    logger.info("Singel segment vector result:")
    printvect(vectorsegment,logger)
    segmentvalue = netValue(data,vectorsegment)
    logger.info(f"Net value single segment: {segmentvalue}")
    


    segments = priceSegments(data,logger)
    vector = ['0']*24
    for i,x in enumerate(segments):
        logger.info('')
        logger.info(f"Segment {i} start: {x['start']} end: {x['end']}")
        xsegment = buildChargeCntrlVector(data[x['start']:x['end']],logger)
        xvalue = netValue(data,xsegment)
        logger.info(f"Value segment {i} {xvalue}")
        if len(xsegment) != 0 and xvalue > 0:
            for i,y in enumerate(xsegment) : 
                if y != '0' : vector[i] = y
        else:
            logger.info("Segment discarded")
    logger.info('')
    logger.info("Multiple segment vector result:")
    printvect(vector,logger)
    msegmentvalue = netValue(data,vector)
    logger.info(f"Net value multiple segment: {msegmentvalue}")
    

    if segmentvalue > msegmentvalue :
        logger.info(f"Single segment vector selected. Net value: {segmentvalue}")
        value = segmentvalue
        returnvector = vectorsegment
    else :
        logger.info(f"Multiple segment vector selected. Net value: {msegmentvalue}")
        value = msegmentvalue
        returnvector = vector
    if value > 0 :
        return returnvector
    else :
        return ['0']*24


#
#
# Following function supports creating a chargevector based on highest and lowest prices.
# The charging vector is an 24 slot list populated with 'L', 'H' and '0' indicating a charging ('L'), discharging ('H') or idling ('0')
# 
# Function can be applied on a segment if data is a subset of a full day data.
# The returned vector will always cover 24 hours.
# 
# The goal is to find CYCLELENGTH highest prices in the segment. Then the price curve  before the first high price will be analyzed to find CYCLELENGTH lowest prices in this interval.
# By this the function will support analysis of a dromedar curve as well as a curve with multiple peaks. However, with multiple peaks only the hours before the first identified high price hour is considered for charging.
#
# An empty charging vector will be returned if it is not possible to charge before peak
#
#
def buildChargeCntrlVector(data,logger):

    firstSegmentHour=int(data[0]['startsAt'][11:13])
    result = buildVector(CYCLELENGTH,CYCLELENGTH,data,logger)
    logger.debug("segmentvector first step:")
    printvectdebug(result['vector'],logger)
    if result['high'] == CYCLELENGTH :
        logger.debug(f"High segment OK, starts at: {result['hindex']}")
    logger.debug(f"Result high {result['high']} high index {result['hindex']} low {result['low']} low index {result['lindex']}")
    
    # Check if we have low segment before high to be able to charge
    if result['low'] < CYCLELENGTH  and result['hindex'] >= CYCLELENGTH:
        logger.debug(f"Full length low segment not found before high. Shorten segment and repeat analysis. Start segment at  {0}, end at {result['hindex']}")
        # Check if we have room for a low segment before high to be able to charge by removing tail of low price hours
        result_low = buildVector(CYCLELENGTH,0,data[0:result['hindex'] - firstSegmentHour],logger) # Make a new sort of earlier hours to find charging hours before discharge hours     +result['high']

        logger.debug("Lower part")
        printvectdebug(result_low['vector'],logger)
        if result_low['low'] < CYCLELENGTH :
            logger.debug("Not possible to fulfill low segment requirement")
            logger.debug(f"Low segment short {result_low['low']}, starts at: {result_low['lindex']}")
            if result_low['low'] <= result['low'] :
                if result['low'] == 0:
                    return []                               # This can't happen or?
                else :
                    return result['vector']                 # Low segment analysis not better than first attempt - return first attempt
            
        logger.debug(f"Low segment found, starts at {result_low['lindex']}")   # Continue and merge first attempt with low segment. 
        for i in range(24) :
            if result_low['vector'][i] == 'L' : result['vector'][i] = result_low['vector'][i]
            if i >=result['hindex']+result['high'] :          #Clear all low hours after discharging hours
                #print (f"Clear tail hour {i}")
                result['vector'][i] = '0'
        return result['vector']
    elif result['hindex'] >= CYCLELENGTH :
        logger.debug(f"Low segment OK, starts at: {result['lindex']}")
        return result['vector']
    else :
        logger.debug("Not possible to fit a low segment before high. Return empty vector")
        return []
            

#
#
# Builds a basic charging vector with the nrlow lowest prices marked with 'L' and nrhigh highest prices marked with 'H'
# Low prices must be found prior to any high price hour. All other hours will be marked with '0'
# 
# Returns the following data structure
# {
# high : n          n equals no of high price hours found
# hindex : no       no index of first in high segment
# low : n           n equals no of low price hours found prior to any high
# lindex : no       no index of first in low segment
# vector: ['L'/'H'/'0']*24
# }
#
#
def buildVector(nrlow,nrhigh,data,logger):
    result = {'high':0,'low':0,'hindex':0,'lindex':0,'vector':['0']*24}
    logger.debug (f"Length of data to be analyzed {len(data)}")
    printdata(data,logger)

    sorted_data = sorted(data, key=lambda d: d['total'])
    logger.debug("Sorted data: ")
    printdata(sorted_data,logger)
    logger.debug(f"nlow {nrlow} nhigh {nrhigh}")

    for x in range(min(nrlow,len(data))):
        logger.debug(f"{int(sorted_data[x]['startsAt'][11:13])}:L")
        hour = int(sorted_data[x]['startsAt'][11:13])
        result['vector'][hour]='L'
    logger.debug('')
    for x in range(min(nrhigh,len(data))):
        logger.debug(f"{int(sorted_data[len(data)-1-x]['startsAt'][11:13])}:H")
        hour = int(sorted_data[len(data)-1-x]['startsAt'][11:13])
        result['vector'][hour]='H' 
    logger.debug('')
    nl = 0
    nh = 0
    for i in range(24) :
        if  result['vector'][i] == 'L' : 
            nl = nl + 1
            if nl == 1 :  result['lindex'] = i
        if result['vector'][i] == 'H' :
            result['hindex'] = i
            break
        
    for i in range(result['hindex'],24): 
        if  result['vector'][i] == 'H' : nh = nh + 1
    
    result['high'] = nh
    result['low'] = nl

    return result

#
#
# This function builds a list of price low-low segments which means the pricecurve in each segment is shaped like dromedar. L
#
#

def priceSegments(data,logger) :


    prices = []
    for x in data : prices.append(x['total'])
    
    #
    # Find all high cost peaks used for discharging
    #
    peaksAndValleys = []
    peaks,_ = find_peaks(prices)
    for i in range(len(peaks)):
        d = {'extreme':peaks[i],'type':'H','start':0,'end':0,'value':0,'hours':0}
        peaksAndValleys.append(d)

    #
    # Find all low cost valleys to be used for charging. 
    #
    inv_prices=[]
    for x in prices:
        inv_prices.append(-x)

    valleys,_ = find_peaks(inv_prices)
    for i in range(len(valleys)):
        d = {'extreme':valleys[i],'type':'L','start':0,'end':0,'value':0,'hours':0}
        peaksAndValleys.append(d)

    #
    # Sort segments based on index for extreme values
    #
    # print("length peaksandvalleys " + str(len(peaksAndValleys)))

    peaksAndValleysSorted=sorted(peaksAndValleys, key=lambda d: d['extreme']) 

    # Patch head and tail if needed (must start low and end high) by adding a virtual valley/peak.

    if peaksAndValleysSorted[0]['type'] == 'H':
        #print("Insert L segment in the beginning")
        peaksAndValleysSorted = [{'extreme':0,'type':'L','start':0,'end':0,'value':0,'hours':0}] + peaksAndValleysSorted
    if peaksAndValleysSorted[-1]['type'] == 'L':
        #print("Insert H segment at end")
        peaksAndValleysSorted = peaksAndValleysSorted + [{'extreme':len(prices)-1,'type':'H','start':0,'end':0,'value':0,'hours':0}]

    #
    # Build a list of low-to-low segments
    #
    segments = []
    nseg = 0
    for i in range(0,len(peaksAndValleysSorted),2):
        if i == 0 : 
            start = 0
        else :
            start = peaksAndValleysSorted[i]['extreme']
        if i + 2 < len(peaksAndValleysSorted) :
            end = peaksAndValleysSorted[i+2]['extreme']
        else :
            end = 24
        segments.append({'start':start,'end':end})
        #print (f"Segment {nseg} start: {segments[nseg]['start']} end: {segments[nseg]['end']}")
        nseg=nseg+1
    return segments

    
#
# This function calculates net value for the specific charging vector and prices
#

def netValue(data,vector) :

    if len(vector) == 0 : return 0

    tempvector=vector.copy()
    if len(tempvector) != 0 and tempvector[NOCHARGEHOUR] == 'L' : tempvector[NOCHARGEHOUR] = '0'

    value = 0
    nolow = 0
    nohigh = 0
    for i in range(24) :
        if tempvector[i] == 'L' and nolow < CYCLELENGTH:                            # Charging: Sum up a max CYCLELENGTH charging ours
            nolow = nolow + 1
            value = value - (data[i]['total'] + NETTRANSFERCOST) * (1+INVERTERLOSS)
            #print(f"Hour {i}, {tempvector[i]}, noLow {nolow}")
        if tempvector[i] == 'H' and nolow > 0:                                      # Discharging: Sum up max #charging hours 
            nolow = nolow - 1
            value = value + data[i]['total'] * (1-INVERTERLOSS)*0.8                 # VAT included in cost (charging hours), but not when you sell.
            #print(f"Hour {i}, {tempvector[i]}, noLow {nolow}")
    
    return value*CHARGINGPOWER




def averagePrice(data) :
    sum= 0
    for x in data :
        sum = sum + x['total']
    return sum/24

def printdata(data,logger):
    for x in data:
        logger.debug(f"{x['startsAt'][11:13]}:{x['total']}")


def printvectdebug(vect,logger):
    logger.debug("0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23")
    line = ''
    for i,x in enumerate(vect):
        if i < 9:
            sp =' '
        else:
            sp = '  '
        line = line  + x + sp
        #logger.debug(sp+x,end=' ')
    logger.debug(line)
    logger.debug('')


def printvect(vect,logger):
    logger.info("0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23")
    line = ''
    for i,x in enumerate(vect):
        if i <9:
            sp =' '
        else:
            sp = '  '
        line = line  + x + sp
        #logger.info(sp+x,end=' ')
    logger.info(line)
    logger.info('')

def testdata(data) :

    #Use input data as default

    testdata = [0]*len(data)        
    for i,x in enumerate(data) :
         testdata[i] = x['total']


    #testdata = [0.3055, 0.2994, 0.2921, 0.2902, 0.296, 0.3117, 0.382, 1.7493, 2.2345, 2.2333, 2.2337, 2.234, 1.9699, 1.75, 1.7498, 1.6685, 1.75, 2.1652, 2.7454, 2.51, 0.9099, 0.6484, 0.5767, 0.5056]
    
    #
    #
    # Camel HLLHHL 
    testdata = [0.455, 0.394, 0.2921, 0.2902, 0.296, 0.3117, 0.382, 1.7493, 2.2345, 2.2333, 2.23, 2.22, 1.9699, 1.75, 2.1, 2.05, 2, 1.9, 1.8, 1.5, 0.9, 0.6, 0.5, 0.4]

    #
    # Camel with low segments spread over day
    #
    testdata = [0.455, 0.394, 0.2921, 0.2902, 0.3, 0.3117, 1.8, 2.0, 1.8, 1.0, 0.2, 1.5, 1.6, 1.75, 2.1, 2.05, 2, 1.9, 1.0, 0.8, 0.3, 0.2, 0.2, 0.2]


    for i,x in enumerate(testdata) :
        data[i]['total'] = x
    return data


def empty(vector):
    if 'L' in vector or 'H' in vector : 
        return False
    else:
        return True
        



        


def main():

    options=get_cmd_line_parameters()           # get command line  
    bLogger=logger(LOGFILE, LOGLEVEL)
    
    haSrv=homeAssistant(privatetokens.HA_URL,privatetokens.HA_TOKEN,bLogger)
   
    bLogger.info("*** Battery control system is starting up ***")
    bLogger.info(f"Logging - Log file: {LOGFILE}, Log level: {LOGLEVEL}, Test: {TEST}, Pricecontrol: {PRICECONTROL}")


    batteryChargeCntrl=haEntity(haSrv,"input_select.battery_mode")

    pdata=json.loads(getPrices(bLogger).text)             # get prices
    vector = buildOptimizedChargeCntrlVector(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['today'],bLogger)
    if len(vector) != 0 and vector[NOCHARGEHOUR] == 'L' : vector[NOCHARGEHOUR] = '0'
    if len(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow']) > 0 and not TEST:
        vector_tomorrow =  buildOptimizedChargeCntrlVector(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'],bLogger)
        if len(vector_tomorrow) > 0 and vector_tomorrow[NOCHARGEHOUR] == 'L': vector_tomorrow[NOCHARGEHOUR] = '0'
    else :
        vector_tomorrow = []

    bLogger.info("Todays vector (at startup):" )
    printvect(vector,bLogger)
    #if empty(vector) : 
    #    bLogger.info("Apply maximize self-consumption")
    #    batteryChargeCntrl.setState('Selfconsumption')
    if vector_tomorrow :
        bLogger.info("Next days vector (at startup):")
        printvect(vector_tomorrow,bLogger)
    else:
        bLogger.info("Tomorrows vector empty... (at startup)")
    battery_mode = batteryChargeCntrl.getState()
    bLogger.info(f"Current battery mode (at startup): {battery_mode}")


    if PRICECONTROL:
        haMaxPrice=haEntity(haSrv,'input_number.max_pris')
        haLevel=haEntity(haSrv,'input_number.niva')
        haHeatingLevel=haEntity(haSrv,'sensor.heating_level')
        maxprice = haMaxPrice.getState()
        level = haLevel.getState()
        heatinglevel=haHeatingLevel.getState()
        bLogger.info(f"Current Max Price (at startup): {maxprice}")
        bLogger.info(f"Current Level (at startup): {level}")
        bLogger.info(f"Current Heating Level (at startup): {heatinglevel}")
        todaysAveragePrice = averagePrice(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['today'])
        bLogger.info(f"Todays average price (at startup): {todaysAveragePrice}")
        if len(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow']) > 0 :
            tomorrowsAveragePrice = averagePrice(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'])
        else :
            tomorrowsAveragePrice = 0
    hour = -1

    if TEST : return
   

    # Continous execution loop
    bLogger.info("Start of control loop")
    while True : 

        # Run once each new hour
        if datetime.datetime.now().hour > hour or (datetime.datetime.now().hour == 0 and hour == 23):
            # New hour
            hour = datetime.datetime.now().hour
            time.sleep(60)                      # Wait one minute to make sure we are well beyond hour boundery

            if hour == 0:
                vector=vector_tomorrow
                if empty(vector) :
                    bLogger.info("No charge/discharge segments. Activate maximize self-consumption mode ")
                    batteryChargeCntrl.setState('Selfconsumption')
                else :
                    bLogger.info(f"New day! Todays plan:")
                    printvect(vector_tomorrow,bLogger)
                if PRICECONTROL :
                    todaysAveragePrice=tomorrowsAveragePrice
                    bLogger.info(f"Todays average price is: {todaysAveragePrice}")
                pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['today'] = pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow']
                pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'] = []
            
            if hour == 15:
                pdata=json.loads(getPrices(bLogger).text)             # get new prices
                bLogger.info("Fetched next days prices, analyzing....")
                vector_tomorrow = buildOptimizedChargeCntrlVector(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'],bLogger)
                if len(vector_tomorrow) != 0 : 
                    if vector_tomorrow[NOCHARGEHOUR] == 'L' : vector_tomorrow[NOCHARGEHOUR] = '0'
                    bLogger.info(f"Next days vector: " )
                    printvect(vector_tomorrow,bLogger)
                else :
                    bLogger.info("Next day will apply maximize self-consumption")
                if PRICECONTROL :
                    tomorrowsAveragePrice = averagePrice(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'])
                    bLogger.info(f"Tomorrows average price: {tomorrowsAveragePrice}")
            if  not empty(vector) :
                battery_mode = batteryChargeCntrl.getState()
                if vector[hour] == '0' and battery_mode != 'Idle' :
                    batteryChargeCntrl.setState('Idle',dict(Today=vector, Tomorrow=vector_tomorrow))
                    bLogger.info("Battery mode set to Idle")
                elif vector[hour] == 'L' and battery_mode != 'Charge' :
                    batteryChargeCntrl.setState('Charge',dict(Today=vector, Tomorrow=vector_tomorrow))
                    bLogger.info("Battery mode set to Charge")
                elif vector[hour] == 'H' and battery_mode != 'Discharge' :
                    batteryChargeCntrl.setState('Discharge',dict(Today=vector, Tomorrow=vector_tomorrow))
                    bLogger.info("Battery mode set to Discharge")
            if PRICECONTROL :
                maxprice = haMaxPrice.getState()
                currentprice =  pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['today'][hour]['total']
                heatinglevel = haHeatingLevel.getState();
                level = haLevel.getState()
                if currentprice > float(maxprice) and heatinglevel != 'Off' :
                    bLogger.info(f"Current price is: {currentprice} Heating level set to: Off")
                    haHeatingLevel.setState('Off')
                elif currentprice > todaysAveragePrice *(1+float(level)) and heatinglevel != 'Eco' :
                    bLogger.info(f"Current price is: {currentprice} Heating level set to: Eco")
                    haHeatingLevel.setState('Eco')
                elif currentprice <= todaysAveragePrice *(1+float(level)) and heatinglevel != 'Normal' :
                    bLogger.info(f"Current price is: {currentprice} Heating level set to: Normal")
                    haHeatingLevel.setState('Normal')
        else :
            time.sleep(60)  
         
main()
    
