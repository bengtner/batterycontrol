# Batterycontrol
A python script to control charging/decharging of home battery based on electriciy price

This script will add functionality to a Home Assistant system running an Huawei Solar Integration (https://github.com/wlcrs/huawei_solar, thanks Thijs W.! ) to control
Huawei Sun SOLAR inverter and battery.

The script must be installed as a detached process running 24/7 on any computer running on the same LAN as Home Assistant. I'm using a separate Raspberry Pi for this,
but if you know how to do it it is possible to run this on the same piece of hardware as Home Assistant.

Home Assitant will do the actual control of charging and discharging of the battery. This script will use the Home Assistant REST inteface to instruct Home Assistant to
charge or discharge the battery. This is done by means of an entity input_select.battery_mode, which can accept the following data: "Charge", "Discharge" or "Idle". 
the automation to implement the battery control i HA is not covered in this repo (for the time beeing)
