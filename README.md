# Batterycontrol
A python script to control charging/decharging of home battery based on electriciy price. The fundamental idea is to charge the battery from grid when electricity 
price is low and discharge the battery to the grid when the price is high. The charging/discharging scheme is optimized to maximize revenue over a single day.

The script also includes support to set the value of a sensor in HA to control the heating level. This sensor is set to "Off", "Normal" or "Eco"
based on actual energy price compared to todays average price.

This script will add functionality to a Home Assistant system running an Huawei Solar Integration (https://github.com/wlcrs/huawei_solar, thanks Thijs W.! ) to control
Huawei Sun SOLAR inverter and battery.

The script must be installed as a detached process running 24/7 on any computer running on the same LAN as Home Assistant. A dedicated Raspberry Pi can be used for 
this, but if you know how to do it it is possible to run this on the same piece of hardware as Home Assistant.

## How it works
Home Assitant will do the actual control of charging and discharging of the battery. This script will use the Home Assistant REST inteface to instruct Home Assistant
to charge or discharge the battery. This is done by means of an entity input_select.battery_mode, which can hold the following states: "Charge", "Discharge" or "Idle". 
Example of associated automation code follows below.

This script implements a timed loop. Once per hour it will look into a charge control vector (a python list) that holds a character in 24 positions, one position 
for each hour. 'H' means high price and will result in discharging of the battery. 'L'indicates low price and battery will be charged. '0' basically means no 
operation, i.e the battery is idle. At 15:00 electricity price for next day is fetched from the broker (in this case Tibber) and the price graph is analyzed to find
peaks and valleys. A planned charge control vector is created based on this data.  Every night at 0:00, next days planned vector will replace the current control 
vector.

## Prerequisites
You need Home Asssitant up and running with the above mentioned Huawei Solar integration. You need to verify the integration works and Home Assistant can read
available sensors in the inverter. You also must setup the Time of Use mode in the inverter using the FusionSolar app connected to the inverter integrated LAN.
You should define a 23 h long discharging segment and the remaining hour as a  charging segment. You should select this hour when the electricity price normally is
high, e.g in the morning. This segment is never used but must be there to make the Time of Use mode work.

You also need to define a helper, input_select.battery_mode with the following states: "Charge", "Discharge", "Idle". Create an automation that selects corresponding
battery mode when selector changes value. Here follows an example of how this YAML code could look like:
```
- alias: charge_battery
  description: Will set battery to charging mode
  trigger:
    - platform: state
      entity_id: input_select.battery_mode
      to: "Charge"
  action:
    - service: select.select_option
      target:
        entity_id: select.battery_working_mode
      data:
        option: "Time Of Use"
    - service: number.set_value
      target:
        entity_id:
          - number.battery_maximum_charging_power
          - number.battery_maximum_discharging_power
      data:
        value: 2500
  mode: single

- alias: discharge_battery
  description: Will set battery to discharging mode
  trigger:
    - platform: state
      entity_id: input_select.battery_mode
      to: "Discharge"
  action:
    - service: select.select_option
      target:
        entity_id: select.battery_working_mode
      data:
        option: "Fully Fed To Grid"
    - service: number.set_value
      target:
        entity_id:
          - number.battery_maximum_charging_power
          - number.battery_maximum_discharging_power
      data:
        value: 2500
  mode: single

- alias: idle_battery
  description: Will keep battery idle by setting max_charge/discharge_power to 0
  trigger:
    - platform: state
      entity_id: input_select.battery_mode
      to: "Idle"
  action:
    - service: input_select.select_option
      target:
        entity_id: select.battery_working_mode
      data:
        option: "Maximise Self Consumption"
    - service: number.set_value
      target:
        entity_id:
          - number.battery_maximum_charging_power
          - number.battery_maximum_discharging_power
      data:
        value: 0
  mode: single
```

## Installation
Update battery.py with your private tokens to access your Home Assistant system and to fetch data from the broker. Run the script in test mode to check everything
works as expected:

`python3 ./battery.py -t`

Please note the script utilizes libraries only available for python3, so you must have this version installed in your system

To run this script as a service (24/7 with automatic start/restart), modify the systemd service profile, battery.service, to reflect the path you to your script.
Install this service by means of the following commands:

```
sudo cp battery.service /etc/systemd/system
sudo systemctl enable battery.service
sudo systemctl start battery.service 
```
