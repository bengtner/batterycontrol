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


#########################################################
#
# Class holding data to communicate with Home Assistant
#
#########################################################   

class homeAssistant:

    def __init__(self,url,token):
    
        #
        #   create object holding server
        #

        self.headers = {
            "Authorization": "Bearer " + token,
            "content-type": "application/json",
        }
        self.url = url

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

        
    def getState(self):
        response = get(self.url + "/api/states/" + self.id, headers=self.headers)
        return json.loads(response.text)['state']

    def setState(self,state):
        payload = {
            "state" : state
        }
        response = post(self.url + "/api/states/" + self.id, headers=self.headers, json=payload )

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

    global  LOGFILE,WAIT,LOGLEVEL,TEST,PRICECONTROL

    parser = argparse.ArgumentParser( description='Battery charging control daemon' )
    parser.add_argument("-v", "--loglevel", help="Log level. DEBUG, WARNING, INFO, ERROR or CRITICAL. Default ERROR.",default='ERROR')
    parser.add_argument("-l", "--logfile", help="Log file. Default " + LOGFILE, default=LOGFILE)
    parser.add_argument("-t", "--test", help="Test mode. Will test HA and TIBBER interface. No loop and nothing set in HA. Logging set to INFO and name set to batterytest.log", action="store_true")
    parser.add_argument("-p", "--pricecontrol", help="Price control. Will control setting of entity input_select.heating_level.", action="store_true")

                        
    args = parser.parse_args()
    LOGFILE = args.logfile
    LOGLEVEL=args.loglevel
    if args.test : 
        LOGFILE = "batterytest.log"
        LOGLEVEL= "INFO"
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

def getPrices():
    authorization = {"Authorization": "Bearer" + privatetokens.TIBBER_TOKEN , "Content-Type":"application/json"}
    gql = '{ "query": "{viewer {homes {currentSubscription {priceInfo {current {total energy tax startsAt} today {total energy tax startsAt} tomorrow { total energy tax startsAt }} }}}}"} '
    response = post("https://api.tibber.com/v1-beta/gql", data=gql, headers=authorization)
    return response
#
# 
#
def buildChargeCntrlVector(data,logger):

    vector =[]
    for i in range(24) : vector.append('0')

    testdata = [0.3055, 0.2994, 0.2921, 0.2902, 0.296, 0.3117, 0.382, 1.7493, 2.2345, 2.2333, 2.2337, 2.234, 1.9699, 1.75, 1.7498, 1.6685, 1.75, 2.1652, 2.7454, 2.51, 0.9099, 0.6484, 0.5767, 0.5056]

    prices = []
    for x in data :
        prices.append(x['total'])

    ##
    #prices = testdata
    ###
    logger.info(prices)

    peaksAndValleys = []


    #
    # Find all high cost peaks used for discharging

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
    # Sort segments based on index for extreme value
    #

    peaksAndValleysSorted=sorted(peaksAndValleys, key=lambda d: d['extreme']) 

    # Patch head and tail if needed (must start low and end high) by adding a virtual valley/peak.

    if peaksAndValleysSorted[0]['type'] == 'H':
        peaksAndValleysSorted = [{'extreme':0,'type':'L','start':0,'end':0,'value':0,'hours':0}] + peaksAndValleysSorted

    if peaksAndValleysSorted[-1]['type'] == 'L':
        peaksAndValleysSorted = peaksAndValleysSorted + [{'extreme':len(prices)-1,'type':'H','start':0,'end':0,'value':0,'hours':0}]
    #
    # Calculate beginning and end of each segment
    #
    if len(peaksAndValleysSorted) == 1:    # Just one segment - segment equeals the full array of prices
        segment['start'] = 0
        segment['end'] = len(prices)
    else :
        for i,segment in enumerate(peaksAndValleysSorted):
            if i == 0 :
                segment['start'] = 0
                segment['end'] = math.ceil((segment['extreme'] + peaksAndValleysSorted[i+1]['extreme']) / 2)
            elif i == len(peaksAndValleysSorted) - 1 : # last segment 
                segment['start'] = peaksAndValleysSorted[i-1]['end']
                segment['end'] = len(prices)
            else :
                segment['start'] =  peaksAndValleysSorted[i-1]['end']
                segment['end'] = math.ceil((segment['extreme'] + peaksAndValleysSorted[i+1]['extreme']) / 2)
    #
    # Create an list of tuples holding hourly prices
    #
    hourprice = []
    for i,x in enumerate(prices):
        hourprice.append({'hour':i,'price':x})

    #
    # Sort prices in each segment and make a sum of the n:th (CYCLELENGTH) highest/lowest values for peak/valley segment. Populate vector
    # with H or L for the peak/low values 
    #
    for i,segment in enumerate(peaksAndValleysSorted):
        if segment['type'] == 'H':
            sorted_segment = sorted(hourprice[segment['start']:segment['end']], key=lambda d: d['price'],reverse=True)
        else :
            sorted_segment = sorted(hourprice[segment['start']:segment['end']], key=lambda d: d['price'])
    #    logger.info("*")
        segmentValue = 0
        for n,y in enumerate(sorted_segment) : 
    #        logger.info(y)
            if n < CYCLELENGTH : 
                segmentValue = segmentValue + y['price']
                vector[y['hour']] = segment['type']
                segment['hours'] = n + 1
        segment['value'] = segmentValue
    #    logger.info(segment)

    #logger.info("*")
    logger.info(vector)

    #
    # Analyze all load and discharge segments. Delete a charge segment unless it is profitable taking network transfer cost and inverter losses into account.
    # Also delete corresponding load segment, unless the cost for this is lower than upcoming load segment.
    #
    
    for i in range(0,len(peaksAndValleysSorted),2):
        chargesegmentlength = peaksAndValleysSorted[i]['end'] - peaksAndValleysSorted[i]['start']
        logger.info(peaksAndValleysSorted[i])
        logger.info(peaksAndValleysSorted[i+1])
        if peaksAndValleysSorted[i+1]['value']*(1-INVERTERLOSS) <= (peaksAndValleysSorted[i]['value']+NETTRANSFERCOST*CYCLELENGTH)*(1+INVERTERLOSS ) and chargesegmentlength > 1  \
            or chargesegmentlength < 2 :
            logger.info("Clear high segment "+ str(peaksAndValleysSorted[i+1]['start'])+" to "+ str(peaksAndValleysSorted[i+1]['end']) )
            for n in range(peaksAndValleysSorted[i+1]['start'],peaksAndValleysSorted[i+1]['end']) :
                vector[n] = '0'
            if i == len(peaksAndValleysSorted) - 2  :           # last segment pair
                logger.info("Clear previous L segment(s)")
                for n in range (peaksAndValleysSorted[i]['end'],-1, -1):
                    if vector[n] == 'L':
                        vector[n] = '0' 
                    elif vector[n] == 'H' :
                        break
            if i < len(peaksAndValleysSorted)/2  and (peaksAndValleysSorted[i+2]['value']/peaksAndValleysSorted[i+2]['hours'] < peaksAndValleysSorted[i]['value']/peaksAndValleysSorted[i]['hours']):
                logger.info("Clear low segment "+ str(peaksAndValleysSorted[i]['start'])+" to "+ str(peaksAndValleysSorted[i]['end']) )
                for n in range(peaksAndValleysSorted[i]['start'],peaksAndValleysSorted[i]['end']) :
                    vector[n] = '0' 
    
    if 'H' not in vector and 'L' not in vector :
        vector = []                     # return empty list if no H or L segment
    logger.info("Cleaned up vector:")
    logger.info(vector)

    #
    # Estimate what revenue will be generated applying this vector for battery control
    #
    cost = 0
    value = 0
    nlow = 0
    for i,x in enumerate(vector):
        if x == 'L' :
            if nlow < CYCLELENGTH :
                cost = cost + (prices[i] + NETTRANSFERCOST)*(1+INVERTERLOSS)*2.5
                nlow = nlow + 1
        if x == 'H':
            if nlow > 0 : 
                nlow = nlow - 1
                value = value + prices[i]*(1-INVERTERLOSS)*2.5
    logger.info("Cost: " + str(cost))
    logger.info("Sales: " + str(value))

    return vector

def averagePrice(data) :
    sum= 0
    for x in data :
        sum = sum + x['total']
    return sum/24



def main():

    global  pLogger,NOCHARGEHOUR
    
    options=get_cmd_line_parameters()           # get command line  
    bLogger=logger(LOGFILE, LOGLEVEL)
    
    haSrv=homeAssistant(privatetokens.HA_URL,privatetokens.HA_TOKEN)
   
    bLogger.info("*** Battery control system is starting up ***")
    bLogger.info("Logging - Log file: %s, Log level: %s", LOGFILE, LOGLEVEL)

    batteryChargeCntrl=haEntity(haSrv,"input_select.battery_mode")
    battery_mode=batteryChargeCntrl.getState()
    bLogger.info("Current battery mode: "+battery_mode)

    pdata=json.loads(getPrices().text)             # get prices
    vector = buildChargeCntrlVector(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['today'],bLogger)
    vector[NOCHARGEHOUR] = '0'
    if len(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow']) > 0 :
        planned_vector =  buildChargeCntrlVector(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'],bLogger)
        planned_vector[NOCHARGEHOUR] = '0'
    else :
        planned_vector = []

    bLogger.info("Todays vector (at startup): " + str(vector) )
    bLogger.info("Next days vector (at startup): " + str(planned_vector) )

    if PRICECONTROL:
        haMaxPrice=haEntity(haSrv,'input_number.max_pris')
        haLevel=haEntity(haSrv,'input_number.niva')
        haHeatingLevel=haEntity(haSrv,'sensor.heating_level')
        maxprice = haMaxPrice.getState()
        level = haLevel.getState()
        heatinglevel=haHeatingLevel.getState()
        bLogger.info("Current Max Price (at startup): " + str(maxprice))
        bLogger.info("Current Level (at startup): " + str(level))
        bLogger.info("Current Heating Level (at startup): "+ heatinglevel)
        todaysAveragePrice = averagePrice(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['today'])
        bLogger.info("Todays average price (at startup): "+str(todaysAveragePrice))
        if len(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow']) > 0 :
            tomorrowsAveragePrice = averagePrice(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'])
        else :
            tomorrowsAveragePrice = 0
    hour = datetime.datetime.now().hour

    if TEST : return
   
    while True : 

        if datetime.datetime.now().hour > hour  or (datetime.datetime.now().hour == 0 and hour==23):         # New hour
            if hour == 23:
                hour = 0
            else:
                hour = hour + 1
            time.sleep(60)                      # Wait one minute to make sure we are well beyond hour boundery

            if hour == 0:
                vector=planned_vector
                if len(vector) == 0 :
                    bLogger.info("Price curve flat. Activate maximize self-consumption mode ")
                    batteryChargeCntrl.setState('Selfconsumption')
                else :
                    bLogger.info("Activate planned vector: "+str(planned_vector))
                if PRICECONTROL :
                    todaysAveragePrice=tomorrowsAveragePrice
                    bLogger.info("Todays average price is: "+str(todaysAveragePrice))
                pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['today'] = pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow']
                pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'] = []
            
            if hour == 15:
                pdata=json.loads(getPrices().text)             # get new prices
                bLogger.info("Fetched next days prices, analyzing....")
                planned_vector = buildChargeCntrlVector(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'],bLogger)
                if len(planned_vector) != 0 : 
                    planned_vector[NOCHARGEHOUR] = '0'
                    bLogger.info("Next days vector: " + str(planned_vector) )
                else :
                    bLogger.info("Next day will apply maximize self-consumption")
                if PRICECONTROL :
                    tomorrowsAveragePrice = averagePrice(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'])
                    bLogger.info("Tomorrows average price: "+str(todaysAveragePrice))
            if len(vector) != 0:
                if vector[hour] == '0' :
                    batteryChargeCntrl.setState('Idle')
                    bLogger.info("Battery mode set to Idle")
                elif vector[hour] == 'L':
                    batteryChargeCntrl.setState('Charge')
                    bLogger.info("Battery mode set to Charge")
                else:
                    batteryChargeCntrl.setState('Discharge')
                    bLogger.info("Battery mode set to Discharge")
            else :
                bLogger.info("No high price segments. Apply maximize self-consumptio")
            if PRICECONTROL :
                maxprice = haMaxPrice.getState()
                level = haLevel.getState()

                currentprice =  pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['today'][hour]['total']
                if currentprice > float(maxprice) :
                    bLogger.info("Current price is: "+str(currentprice)+" Heating level set to: Off")
                    haHeatingLevel.setState('Off')
                elif currentprice > todaysAveragePrice *(1+float(level)) :
                    bLogger.info("Current price is: "+str(currentprice)+" Heating level set to: Eco")
                    haHeatingLevel.setState('Eco')
                else :
                    bLogger.info("Current price is: "+str(currentprice)+" Heating level set to: Normal")
                    haHeatingLevel.setState('Normal')
        else :
            time.sleep(60)  
        
main()
    