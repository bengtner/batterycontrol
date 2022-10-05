# Batterycontrol
A python script to control charging/decharging of home battery based on electriciy price. The fundamental idea is to charge the battery from grid when electricity price 
is low and discharge the battery to the grid when the price is high. The charging/discharging scheme is optimized to maximize revenue over a single day.

This script will add functionality to a Home Assistant system running an Huawei Solar Integration (https://github.com/wlcrs/huawei_solar, thanks Thijs W.! ) to control
Huawei Sun SOLAR inverter and battery.

The script must be installed as a detached process running 24/7 on any computer running on the same LAN as Home Assistant. I'm using a separate Raspberry Pi for this,
but if you know how to do it it is possible to run this on the same piece of hardware as Home Assistant.

Home Assitant will do the actual control of charging and discharging of the battery. This script will use the Home Assistant REST inteface to instruct Home Assistant to
charge or discharge the battery. This is done by means of an entity input_select.battery_mode, which can accept the following data: "Charge", "Discharge" or "Idle". 
The automation to implement the battery control i HA is not covered in this repo (for the time beeing)

This script implements a timed loop. Once per hour it will look into a charge control vector (a python list) that holds one character in 24 positions, one position 
for each hour. 'H' means high price and will result in discharging of the battery. 'L'indicates low price and battery will be charged. '0' basically means no operation,
i.e the battery is idle. At 15:00 electricity price for next day is fetched from the broker (in this case Tibber) and the price graph is analyzed to find peaks and 
valleys.
