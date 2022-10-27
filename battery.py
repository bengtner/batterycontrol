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

    global LOGFILE,LOGLEVEL,TEST,PRICECONTROL

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

def buildChargeCntrlVector(data,logger):

    #print(data)

    vector = buildChargeCntrlVectorCamel(data,logger)
    logger.info(f"Camel: {vector}")
    if len(vector) != 0 : 
        camelvalue = netValue(data,vector)
    else : camelvalue = 0
    logger.info(f"Net value camel: {camelvalue}")

    vectorflat = buildChargeCntrlVectorFlat(data,logger)
    logger.info (f"Flat: {vectorflat}")
    if len(vectorflat)!=0 :
        flatvalue = netValue(data,vectorflat)
    else :
        flatvalue = 0
    logger.info(f"Net value flat: {flatvalue}")

    if flatvalue > camelvalue :
        logger.info(f"Flat vector selected.")
        return vectorflat
    else :
        logger.info(f"Camel vector selected.")
        return vector


##############################
#
# Following functions support creating a chargevector based on highest and lowest prices
#
def buildChargeCntrlVectorFlat(data,logger):

    vector = buildVector(CYCLELENGTH,CYCLELENGTH,data)
    print ("Flatvector first step:")
    print(vector)
    hindex = findSegment('H',CYCLELENGTH,vector)    # Find the latest H segment with CYCLELENGTH no of H
    print("High index OK, starts at: "+ str(hindex))
    if hindex < 0:
        #print ("High segment not found with this algorithm. No vector returned")
        return []
    # Check if we have low segment before high to be able to charge
    lindex = findSegment('L',CYCLELENGTH,vector[0:hindex])
    if lindex < 0:
            print("Low segment not found before high. Shorten segment and repeat analysis")
            # Check if we have low segment before high to be able to charge
            low_vector = buildVector(CYCLELENGTH,0,data[0:hindex]) # Make a new sort of earlier hours 
            print("Lower part")
            print(low_vector)
            lindex = findSegment('L',CYCLELENGTH,low_vector[0:hindex])
            if lindex < 0:
                print("Low segment not found before high. No vector returned")     
                return []
            else :
                print ("Low segment found before high")
                print (f"Low segment OK, starts at {lindex}")
                for i in range(0,hindex) :
                   if low_vector[i] == 'L' : vector[i] = 'L'
                return vector


    else :
        print (f"Low segment OK, starts at {lindex}")
    #    for i in range(0,hindex) :
    #        if low_vector[i] == 'L' : vector[i] = 'L'
    #    for i in range(len(data) - 1,hindex,-1) :
    #        if vector[i] == 'L' :
    #            vector[i] = '0'
        return vector
            


def findSegment(item,length,vector):

    try:
        pos=len(vector) - vector[::-1].index(item) -1
    except ValueError as e:
        pos = -1
        return pos
    nitem = 0
    for i in range(pos,-1,-1) :
        if vector[i] == item :
            nitem = nitem +1
            if nitem >= length :
                return i
    if vector[0] == item : 
        pos = 0 
    else : 
        pos = -1
    return pos


def buildVector(nrlow,nrhigh,data):


    vector = ['0']*24
    print (f"Length of data to be analyzed {len(data)}")
    printdata(data)

    sorted_data = sorted(data, key=lambda d: d['total'])
    print("Sorted data: ")
    printdata(sorted_data) 
    print (f"nlow {nrlow} nhigh {nrhigh}")

    for x in range(min(nrlow,len(data))):
        print(f"L {int(sorted_data[x]['startsAt'][11:13])}")
        hour = int(sorted_data[x]['startsAt'][11:13])
        vector[hour]='L'
    for x in range(min(nrhigh,len(data))):
        hour = int(sorted_data[len(data)-1-x]['startsAt'][11:13])
        vector[hour]='H' ## 4 lång, kl 23 -> 3

    return vector
#################################



def netValue(data,vector) :

    tempvector=vector.copy()
    if len(tempvector) != 0 and tempvector[NOCHARGEHOUR] == 'L' : tempvector[NOCHARGEHOUR] = '0'

    value = 0
    nolow = 0
    nohigh = 0
    for i in range(24) :
        if tempvector[i] == 'L' and nolow < CYCLELENGTH:
            nolow = nolow + 1
            value = value - data[i]['total'] * (1+INVERTERLOSS)
            #print(f"Hour {i}, {tempvector[i]}, noLow {nolow}")
        if tempvector[i] == 'H' and nolow > 0:
            nolow = nolow - 1
            value = value + data[i]['total'] * (1-INVERTERLOSS)
            #print(f"Hour {i}, {tempvector[i]}, noLow {nolow}")
    
    return value*CHARGINGPOWER


def buildChargeCntrlVectorCamel(data,logger):


    vector = ['0']*24

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
    # Sort segments based on index for extreme values
    #
    # print("length peaksandvalleys " + str(len(peaksAndValleys)))

    peaksAndValleysSorted=sorted(peaksAndValleys, key=lambda d: d['extreme']) 

    # Patch head and tail if needed (must start low and end high) by adding a virtual valley/peak.

    if peaksAndValleysSorted[0]['type'] == 'H':
        peaksAndValleysSorted = [{'extreme':0,'type':'L','start':0,'end':0,'value':0,'hours':0}] + peaksAndValleysSorted

    if peaksAndValleysSorted[-1]['type'] == 'L':
        peaksAndValleysSorted = peaksAndValleysSorted + [{'extreme':len(prices)-1,'type':'H','start':0,'end':0,'value':0,'hours':0}]
    
    # Remove this!!!!
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
    # Alternative low-to-low segments
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
        print (f"Segment {nseg} start: {segments[nseg]['start']} end: {segments[nseg]['end']}")
        nseg=nseg+1
    

    vectorflat = ['0']*24
    for i,x in enumerate(segments):
        print (f"Segment: {i} start: {x['start'] } end: {x['end']}")
        vectx=buildChargeCntrlVectorFlat(data[x['start']:x['end']],logger)
        if len(vectx) != 0:
            for n in range(24):
                if vectx[n] != '0': vectorflat[n] = vectx[n]
            print("Flat segment based vector:")
            print(vectorflat)
            #if (i <  len(segments) -1) :# there is at least one more segment that can be prolonged
            #    # extend next segment to include tail of this segment.
            #    print(f"Extend next segment {i+1} with tail of current segment by decreasing starting index")
            #    print(f"Scan from {x['end']-1}")
            #    for n in range(x['end']-1,x['start']-1,-1):
            #        print(f"Index {n}")
            #        if vectorflat[n] == 'H' :
            #            print(f"End of high found at {n}, set start of next segment to {n+1}")
            #            segments[i+1]['start'] =  n + 1
            #            break
                        
                
        else:
            if i < len(segments) - 1 :  
                print (f"Segment {i} not used. Next segment extended")
                segments[i+1]['start'] = x['start']
            else :
                print("Last segment not used")
        
    
    #
    # Create an list of tuples holding hourly prices
    #
    hourprice = []
    for i,x in enumerate(prices):
        hourprice.append({'hour':i,'price':x})

    #
    # Sort price
    # s in each segment and make a sum of the n:th (CYCLELENGTH) highest/lowest values for peak/valley segment. Populate vector
    # with H or L for the peak/low values 
    #
    for i,segment in enumerate(peaksAndValleysSorted):
        if segment['type'] == 'H':
            sorted_segment = sorted(hourprice[segment['start']:segment['end']], key=lambda d: d['price'],reverse=True)
        else :
            sorted_segment = sorted(hourprice[segment['start']:segment['end']], key=lambda d: d['price'])
        segmentValue = 0
        for n,y in enumerate(sorted_segment) : 
            if n < CYCLELENGTH : 
                segmentValue = segmentValue + y['price']
                vector[y['hour']] = segment['type']
                segment['hours'] = n + 1
        segment['value'] = segmentValue
        print (f"CamelSegment: {i} Type: {segment['type']} Start: {segment['start']} End: {segment['end']} Extreme: {segment['extreme']} Value: {segment['value']}")
    #
    # Analyze all load and discharge segments. Delete a charge segment unless it is profitable taking network transfer cost and inverter losses into account.
    # Also delete corresponding load segment, unless the cost for this is lower than upcoming load segment.
    #

    vectorflat = ['0']*24
    for i in range(0,len(peaksAndValleysSorted),2):
        print (f"Segment: {i} start: {peaksAndValleysSorted[i]['start'] } end: {peaksAndValleysSorted[i+1]['end']}")
        vectx=buildChargeCntrlVectorFlat(data[ peaksAndValleysSorted[i]['start']:peaksAndValleysSorted[i+1]['end']],logger)
        if len(vectx) != 0:
            for n in range(24):
                if vectx[n] != '0': vectorflat[n] = vectx[n]
            print("Flat vector:")
            print(vectorflat)


    
    for i in range(0,len(peaksAndValleysSorted),2):
        chargesegmentlength = peaksAndValleysSorted[i]['end'] - peaksAndValleysSorted[i]['start']
        logger.info(peaksAndValleysSorted[i])
        logger.info(peaksAndValleysSorted[i+1])
        print ("Segment "+str(i) + " chg seg length " + str(chargesegmentlength)+" High segment value " + str(peaksAndValleysSorted[i+1]['value']*(1-INVERTERLOSS)) + " Low segment value " + str((peaksAndValleysSorted[i]['value']+NETTRANSFERCOST*chargesegmentlength)*(1+INVERTERLOSS )))
        if (peaksAndValleysSorted[i+1]['value']*(1-INVERTERLOSS) <= (peaksAndValleysSorted[i]['value']+NETTRANSFERCOST*min(chargesegmentlength,CYCLELENGTH))*(1+INVERTERLOSS )) and chargesegmentlength > 1  \
            or chargesegmentlength < 2 :
            logger.info(f"Clear high segment {peaksAndValleysSorted[i+1]['start']} to {peaksAndValleysSorted[i+1]['end']}" )
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
                logger.info(f"Clear low segment {peaksAndValleysSorted[i]['start']} to {peaksAndValleysSorted[i]['end']}" )
                for n in range(peaksAndValleysSorted[i]['start'],peaksAndValleysSorted[i]['end']) :
                    vector[n] = '0' 
    
    if 'H' not in vector and 'L' not in vector :
        vector = []                     # return empty list if no H or L segment

    return vector

def averagePrice(data) :
    sum= 0
    for x in data :
        sum = sum + x['total']
    return sum/24

def printdata(data):
    for x in data:
        print(f"{x['startsAt'][11:13]}:{x['total']}")


def main():

    options=get_cmd_line_parameters()           # get command line  
    bLogger=logger(LOGFILE, LOGLEVEL)
    
   # haSrv=homeAssistant(privatetokens.HA_URL,privatetokens.HA_TOKEN)
   
    bLogger.info("*** Battery control system is starting up ***")
    bLogger.info("Logging - Log file: %s, Log level: %s", LOGFILE, LOGLEVEL)

    #batteryChargeCntrl=haEntity(haSrv,"input_select.battery_mode")
    #battery_mode=batteryChargeCntrl.getState()
    #bLogger.info("Current battery mode: "+battery_mode)

    pdata=json.loads(getPrices().text)             # get prices
    vector = buildChargeCntrlVector(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['today'],bLogger)
    if len(vector) != 0 and vector[NOCHARGEHOUR] == 'L' : vector[NOCHARGEHOUR] = '0'
    if len(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow']) > 0 :
        a=1
        planned_vector =  buildChargeCntrlVector(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'],bLogger)
        if len(planned_vector) > 0 and planned_vector[NOCHARGEHOUR] == 'L': planned_vector[NOCHARGEHOUR] = '0'
    else :
        planned_vector = []

    bLogger.info(f"Todays vector (at startup): {vector}" )
    bLogger.info(f"Next days vector (at startup): {planned_vector}" )

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
                    bLogger.info(f"Activate planned vector: {planned_vector}")
                if PRICECONTROL :
                    todaysAveragePrice=tomorrowsAveragePrice
                    bLogger.info(f"Todays average price is: {todaysAveragePrice}")
                pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['today'] = pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow']
                pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'] = []
            
            if hour == 15:
                pdata=json.loads(getPrices().text)             # get new prices
                bLogger.info("Fetched next days prices, analyzing....")
                planned_vector = buildChargeCntrlVector(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'],bLogger)
                if len(planned_vector) != 0 : 
                    if planned_vector[NOCHARGEHOUR] == 'L' : planned_vector[NOCHARGEHOUR] = '0'
                    bLogger.info(f"Next days vector: {planned_vector}" )
                else :
                    bLogger.info("Next day will apply maximize self-consumption")
                if PRICECONTROL :
                    tomorrowsAveragePrice = averagePrice(pdata['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['tomorrow'])
                    bLogger.info(f"Tomorrows average price: {tomorrowsAveragePrice}")
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
                    bLogger.info(f"Current price is: {currentprice} Heating level set to: Off")
                    haHeatingLevel.setState('Off')
                elif currentprice > todaysAveragePrice *(1+float(level)) :
                    bLogger.info(f"Current price is: {currentprice} Heating level set to: Eco")
                    haHeatingLevel.setState('Eco')
                else :
                    bLogger.info(f"Current price is: {currentprice} Heating level set to: Normal")
                    haHeatingLevel.setState('Normal')
        else :
            time.sleep(60)  
        
main()
    
