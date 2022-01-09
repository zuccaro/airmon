#!/usr/bin/python3

""" Reads stemma qt boards and reports on air quality/temp, logs """

import argparse
import json
import logging
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from prometheus_client import start_http_server, Summary, Gauge
from prometheus_client.core import REGISTRY

import adafruit_pct2075
import adafruit_rgb_display.st7789 as st7789
import adafruit_scd4x
import board
import busio
import digitalio
from adafruit_bme280 import basic as adafruit_bme280
from adafruit_pm25.i2c import PM25_I2C
from digitalio import DigitalInOut, Direction, Pull
from PIL import Image, ImageColor, ImageDraw, ImageFont


def get_ip():
    """ gets ip (https://stackoverflow.com/questions/166506/finding-local-ip-addresses-using-pythons-stdlib) """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1)) # doesn't even have to be reachable
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def init_display(disp):
    """ initializes display """
    height = disp.width  # we swap height/width to rotate it to landscape!
    width = disp.height
    image = Image.new("RGB", (width, height))
    rotation = 90
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, height), outline=0, fill=(0, 0, 0))
    disp.image(image, rotation)
    return (image,draw)

def clear_disp(drawing, display, image):
    """ clears screen """
    height = display.width  # we swap height/width to rotate it to landscape!
    width = display.height
    rotation = 90
    drawing.rectangle((0, 0, width, height), outline=0, fill=(0, 0, 0))
    display.image(image, rotation)

def backlight(x=True):
    """ toggles backlight on or off """
    b = digitalio.DigitalInOut(board.D22)
    b.switch_to_output()
    b.value = x

def get_pm_description(val):
    if (val < 51):
        return 'good'
    elif (val < 101):
        return 'moderate'
    elif (val < 151):
        return 'usg'
    elif (val < 201):
        return 'unhealthy'
    elif (val < 300):
        return 'very unhealthy'
    elif (val < 500):
        return 'hazardous'

def celsius2fahrenheit(celsius):
    """ converts celsius to fahrenheit """
    return (celsius * 1.8) + 32

def particles2color(pm10std, pm25std, pm100std):
    """ attempts to convert PM1.0, PM2.5, and PM10 into a color """
    c = ImageColor.getrgb('green')
    if (pm25std>2 or pm100std>2):
        c = ImageColor.getrgb('olive')
    elif (pm25std>12 or pm100std>55):
        c = ImageColor.getrgb('yellow')
    elif (pm25std>35.4 or pm100std>155):
        c = ImageColor.getrgb('orange')
    elif (pm25std>55.4 or pm100std>254):
        c = ImageColor.getrgb('red')
    elif (pm25std>150 or pm100std>355):
        c = ImageColor.getrgb('purple')
    elif (pm25std>250 or pm100std>425):
        c = ImageColor.getrgb('magenta')
    return c

class SensorDataServer(BaseHTTPRequestHandler):
    def do_GET(self):
        global data
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=True).encode('utf-8'))

def serve_forever(httpd):
    """ starts web api """
    httpd.serve_forever()

def serve_prometheus(port):
    """ starts prometheus exporter """
    start_http_server(port)

def main():
    global data

    default_wsport = 9098
    default_promport = 9099
    hostname = socket.gethostname()
    data = { 'airmon_time' : datetime.now().isoformat(), 'airmon_station':hostname }
    parser = argparse.ArgumentParser(description='Monitors air quality')
    parser.add_argument('--creds', '-c', type=str, help='google cloud credentials file', default='google-creds.json')
    parser.add_argument('--debug', '-d', action='store_true') # only in python 3.9: action=argparse.BooleanOptionalAction)
    parser.add_argument('--log-interval', '-l',dest='ginterval', type=int, default=300, help='seconds to wait between sending to google sheet')
    parser.add_argument('--wsport','-w',type=int, default=default_wsport, help=f'web service api port (default:{default_wsport})')
    parser.add_argument('--interval','-i',type=int, default=5, help=f'time to sleep between sensor readings (default:5)')
    parser.add_argument('--prometheus','--prom','-p', default=default_promport,help=f'prometheus api port (default:{default_promport})')
    parser.add_argument('--google','-g', default=False, help='log to google sheet')
    parser.add_argument('--name','-n', default=hostname, help=f'name of this station (default:{hostname})')
    parser.add_argument('--scd40', help='enable SCD40 CO2 monitor',action=argparse.BooleanOptionalAction)
    parser.add_argument('--pct2075', help='enable PCT2075 temperature monitor',action=argparse.BooleanOptionalAction)
    parser.add_argument('--bme280', help='enable BME280 temperature monitor',action=argparse.BooleanOptionalAction)
    parser.add_argument('--st7789', help='enable BME280 temperature monitor', action=argparse.BooleanOptionalAction)
    parser.add_argument('--pm25', help='enable BME280 temperature monitor', action=argparse.BooleanOptionalAction)
    args = parser.parse_args()

    TEMP = Gauge('airmon_temperature', 'Temperature (degrees Fahrenheit)', ['station'])
    HUMIDITY = Gauge('airmon_humidity', 'Humidity (percentage)', ['station'])
    PRESSURE = Gauge('airmon_pressure', 'Barometric pressure (hPa)', ['station'])
    CO2 = Gauge('airmon_co2', 'CO2 parts per million (400ppm – 2000ppm)', ['station'])
    PM10 = Gauge('airmon_pm1', 'Ultrafine particulate matter (PM1.0)', ['station'])
    PM25 = Gauge('airmon_pm25', 'Fine particulate matter (PM2.5)', ['station'])
    PM100 = Gauge('airmon_pm10', 'Particulate matter (PM10.0)', ['station'])

    level = logging.INFO
    if args.debug:
        level = logging.DEBUG

    logging.basicConfig(level=level)

    logging.info(args)
    ip = get_ip()
    logging.debug(f'host is {hostname} ({ip})')

    # web server
    if args.wsport:
        thread = Thread(target=serve_forever, args=(socketserver.TCPServer(("", args.wsport), SensorDataServer), ))
        thread.setDaemon(True)
        thread.start()
        logging.info(f'started web service thread, listening at http://*:{args.wsport}')

    # prometheus exporter
    if args.prometheus:
        thread2 = Thread(target=serve_prometheus, args=(args.prometheus,))
        thread2.setDaemon(True)
        thread2.start()
        logging.info(f'started prometheus thread, listening at http://*:{args.prometheus}')

    # google sheets exporter
    if args.google:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
        scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(args.creds, scope)
        client = gspread.authorize(creds)
        client.login()
        gsheet = client.open("Air Quality Log") #workbook containing air quality data
        sheet = gsheet.worksheet('RawData') #sheet where data is logged
        logging.info('initialized worksheet in google cloud')

    when = time.localtime()

    reset_pin = None

    # init mini tft
    disp=None
    if args.st7789:
        cs_pin = digitalio.DigitalInOut(board.CE0)
        dc_pin = digitalio.DigitalInOut(board.D25)
        BAUDRATE = 64000000
        spi = board.SPI()
        disp = st7789.ST7789(spi,cs=cs_pin,dc=dc_pin,rst=reset_pin,baudrate=BAUDRATE,width=135,height=240,x_offset=53,y_offset=40)
        logging.debug(f'initialized display {disp}')

    # Create i2c 
    i2c = busio.I2C(board.SCL, board.SDA, frequency=100000)
    logging.debug(f'initialized i2c bus @100KHz')

    # Connect to a PM2.5 sensor over I2C
    pm25=None
    if args.pm25:
        pm25 = PM25_I2C(i2c, reset_pin)
        logging.debug(f'initialized pm25 (0x12)')

    # Connect to CO2 (SCD 40) sensor over I2C
    scd4x = None
    if args.scd40:
        scd4x = adafruit_scd4x.SCD4X(i2c)
        logging.debug(f'initialized SCD40 (0x62)')

    #free temperature sensor (only have 1)
    pct = None
    if args.pct2075:
        try:
            pct = adafruit_pct2075.PCT2075(i2c)
            pct.high_temperature_threshold = 35.5
            pct.temperature_hysteresis = 30.0
            pct.high_temp_active_high = False
            logging.debug(f"Found PCT2075 sensor {pct}")
        except:
            logging.fatal(f"Failed to locate PCT2075 sensor!")
            sys.exit(1)

    #2 buttons on pioled
    buttonA = digitalio.DigitalInOut(board.D23)
    buttonB = digitalio.DigitalInOut(board.D24)
    buttonA.switch_to_input(pull=digitalio.Pull.DOWN)
    buttonB.switch_to_input(pull=digitalio.Pull.DOWN)

    bme280 = None
    if args.bme280:
        try:
            bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c)
            logging.debug(f'Initialized BME280 0x77')
        except:
            logging.fatal(f'Failed to initialize BME280')
            sys.exit(1)

    screen = False
    if disp:
        image, draw = init_display(disp)
        logging.debug('Initialized OLED display')
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        clear_disp(draw, disp, image)
        backlight(True)
        screen = True

        # First define some constants to allow easy resizing of shapes.
        padding = -2
        top = padding
        width = disp.height
        height = disp.width  # we swap height/width to rotate it to landscape!
        bottom = height - padding

        # Move left to right keeping track of the current x position for drawing shapes.
        x = 0
        rotation = 90

    logged_time = None

    if scd4x:
        scd4x.start_periodic_measurement()

    airqual = None
    co2 = None
    interval = args.interval
    if scd4x:
        interval += 5
    logging.info(f'starting measurement loop (reading every {interval}s)')

    while True:
        time.sleep(interval) # lets start with a break (SCD40 needs a little time to warm up)

        if scd4x:
            while not(scd4x.data_ready):
                logging.debug('SCD40 CO2 monitor not ready')
                time.sleep(1)
            co2 = scd4x.CO2

        pm10std=pm25std=pm100std=None
        part3=part5=part10=part25=part50=part100=None
        pm25def=pm100def=pmstd=None
        if pm25:
            try:
                airqual = pm25.read()
                pm10std = int(airqual["pm10 standard"])
                pm25std = int(airqual["pm25 standard"])
                pm100std = int(airqual["pm100 standard"])
                part3 = airqual["particles 03um"]
                part5 = airqual["particles 05um"]
                part10 = airqual["particles 10um"]
                part25 = airqual["particles 25um"]
                part50 = airqual["particles 50um"]
                part100 = airqual["particles 100um"]
                pm25def = get_pm_description(pm25std)
                pm100def = get_pm_description(pm100std)
                pmstd = f'PM2.5={pm25std} {pm25def} 10={pm100std} {pm100def} CO2={co2}'
            except RuntimeError:
                logging.warning("Unable to read from air quality sensor, retrying...")
                continue

        tx = time.localtime()
        dt = datetime.now()
        t = time.strftime('%Y%m%d,%H:%M:%S', tx)
        tday = time.strftime('%Y-%m-%d', tx)
        ttime = time.strftime('%H:%M:%S', tx)

        tempstr = "No temperature data"
        ftemp = None
        humidity = None
        if bme280:
            ftemp = celsius2fahrenheit(bme280.temperature)
            humidity = bme280.humidity
            tempstr =  f'{ftemp:.2f}°F hum={bme280.humidity:.2f}% pres={bme280.pressure:.2f}kPa'

        if pct:
            ftemp = celsius2fahrenheit(pct.temperature)
            tempstr =  f'{ftemp:.2f}°F'

        data = {}
        data.update({'airmon_time':dt.isoformat()})

        if co2:
            data.update({'airmon_co2':co2}) #wsapi
            CO2.labels(args.name).set(co2)  #prom

        if ftemp:
            data.update({'airmon_temp':ftemp}) #wsapi
            TEMP.labels(args.name).set(ftemp) #prom

        if bme280:
            data.update({'airmon_humidity':bme280.humidity}) #wsapi
            data.update({'airmon_pressure':bme280.pressure}) #wsapi
            HUMIDITY.labels(args.name).set(bme280.humidity) #prom
            PRESSURE.labels(args.name).set(bme280.pressure) #prom

        if airqual:
            for k in airqual:
                data['airmon_'+k.replace(' ','_')] = airqual[k] #wsapi
            PM10.labels(args.name).set(pm10std) #prom
            PM25.labels(args.name).set(pm25std) #prom
            PM100.labels(args.name).set(pm100std) #prom
            parts1 = f'.3μm:{part3} .5μm:{part5} 1μm:{part10}'
            parts2 = f'2.5μm:{part25} 5μm:{part50} 10μm:{part100}'
            logging.debug(f'{ttime} {tempstr} {pmstd} {parts1} {parts2}')
        else:
            logging.debug(f'{ttime} {tempstr}')

        values = [tday, ttime, ftemp, humidity, pm10std, pm25std, pm100std, part3, part5, part10, part25, part50, part100, hostname, co2]
        lines = [f'{tday} {ttime}',f'{hostname} ({ip})',tempstr,pmstd]

        if args.google:
            if ((logged_time is None) or ((time.mktime(tx)-time.mktime(logged_time)) > args.ginterval)):
                logging.info(f'{ttime} logging to gsheet every {args.ginterval}s')
                logging.debug(f'{tday},{ttime},{ftemp:.2f},{bme280.humidity:.2f},{pm10std},{pm25std},{pm100std},{part3},{part5},{part10},{part25},{part50},{part100},{co2}')
                try:
                    sheet.append_row(values)
                    logged_time = tx
                except:
                    logging.warn('failed to append values to google sheet')
            else:
                tam = args.interval - (time.mktime(tx)-time.mktime(logged_time))
                lines.append(f'next logging: {tam}')

        if screen:
            c = None
            c = particles2color(pm10std, pm25std, pm100std)
            draw.rectangle((0, 0, width, height), outline=0, fill=c)
            y = top
            for line in lines:
                draw.text((x, y), line, font=font, fill="#FFFFFF")
                y += font.getsize(line)[1]
            disp.image(image, rotation)

        if disp:
            if not (buttonA.value):
                screen = True
                backlight(True)
            if not (buttonB.value):
                screen = False
                backlight(False)
            if not (buttonA.value) and not (buttonB.value): #gtfo
                break

    if scd4x:
        scd4x.stop_periodic_measurement()

if __name__=='__main__':
    main()
