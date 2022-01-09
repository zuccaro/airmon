#!/usr/bin/python3

""" Reads stemma qt boards and reports on air quality/temp, logs """

import argparse
import http.server
import logging
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time

import adafruit_pct2075
import adafruit_rgb_display.st7789 as st7789
import adafruit_scd4x
import board
import busio
import digitalio
import gspread
from adafruit_bme280 import basic as adafruit_bme280
from adafruit_pm25.i2c import PM25_I2C
from digitalio import DigitalInOut, Direction, Pull
from oauth2client.service_account import ServiceAccountCredentials
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

def main():
    global data
    data = {}
    parser = argparse.ArgumentParser(description='Monitors air quality')
    parser.add_argument('--creds', '-c', type=str, help='google cloud credentials file', default='google-creds.json')
    parser.add_argument('--debug', '-d', action='store_true') # only in python 3.9, action=argparse.BooleanOptionalAction)
    parser.add_argument('--log-interval', '-l',dest='interval', type=int, default=300, help='seconds to wait between sending to google sheet')
    parser.add_argument('--wsport','-w',type=int,default=8098,help='web service api port (default:8098)')

    args = parser.parse_args()

    level = logging.INFO
    if args.debug:
        level = logging.DEBUG

    logging.basicConfig(level=level)

    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        print("serving at port", PORT)
        httpd.serve_forever()

    scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name(args.creds, scope)
    client = gspread.authorize(creds)
    client.login()

    gsheet = client.open("Air Quality Log") #workbook containing air quality data
    sheet = gsheet.worksheet('RawData') #sheet where data is logged
    logging.info('initialized worksheet in cloud')

    hostname = socket.gethostname()
    ip = get_ip()
    logging.info(f'host is {hostname} ({ip})')

    when = time.localtime()

    # init mini tft
    cs_pin = digitalio.DigitalInOut(board.CE0)
    dc_pin = digitalio.DigitalInOut(board.D25)
    reset_pin = None
    BAUDRATE = 64000000
    spi = board.SPI()
    disp = st7789.ST7789(spi,cs=cs_pin,dc=dc_pin,rst=reset_pin,baudrate=BAUDRATE,width=135,height=240,x_offset=53,y_offset=40)
    logging.debug(f'initialized display {disp}')

    # Create library object, use 'slow' 100KHz frequency!
    i2c = busio.I2C(board.SCL, board.SDA, frequency=100000)

    # Connect to a PM2.5 sensor over I2C
    pm25 = PM25_I2C(i2c, reset_pin)
    logging.debug(f'initialized pm25 {pm25}')

    # Connect to CO2 (SCD 40) sensor over I2C
    scd4x = adafruit_scd4x.SCD4X(i2c)
    logging.debug(f'initialized SCD40 {scd4x}')

    #free temperature sensor (only have 1)
    #pct = adafruit_pct2075.PCT2075(i2c)
    #print("Found PCT2075 sensor")

    #2 buttons on pioled
    buttonA = digitalio.DigitalInOut(board.D23)
    buttonB = digitalio.DigitalInOut(board.D24)
    buttonA.switch_to_input(pull=digitalio.Pull.DOWN)
    buttonB.switch_to_input(pull=digitalio.Pull.DOWN)

    bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c)
    logging.debug(f'initialized BME280 {bme280}')

    image, draw = init_display(disp)
    logging.debug('initialized OLED display')
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

    scd4x.start_periodic_measurement()

    while True:

        time.sleep(5) # lets start with a break (SCD40 needs a little time to warm up)

        while not(scd4x.data_ready):
            logging.debug('SCD40 CO2 monitor not ready')
            time.sleep(1)

        co2 = scd4x.CO2

        try:
            aqdata = pm25.read()
        except RuntimeError:
            logging.warning("Unable to read from sensor, retrying...")
            continue

        tx = time.localtime()

        t = time.strftime('%Y%m%d,%H:%M:%S', tx)
        tday = time.strftime('%Y-%m-%d', tx)
        ttime = time.strftime('%H:%M:%S', tx)
        ftemp = celsius2fahrenheit(bme280.temperature)
        tempstr =  f'{ftemp:.2f}°F {bme280.humidity:.2f}% hum'
        pm10std = int(aqdata["pm10 standard"])
        pm25std = int(aqdata["pm25 standard"])
        pm100std = int(aqdata["pm100 standard"])
        pm25def = get_pm_description(pm25std)
        pm100def = get_pm_description(pm100std)
        pmstd = f'PM2.5={pm25std} {pm25def} 10={pm100std} {pm100def} CO2={co2}PPM'
        pm10env = aqdata["pm10 env"]
        pm25env = aqdata["pm25 env"]
        pm100env = aqdata["pm100 env"]
        part3 = aqdata["particles 03um"]
        part5 = aqdata["particles 05um"]
        part10 = aqdata["particles 10um"]
        part25 = aqdata["particles 25um"]
        part50 = aqdata["particles 50um"]
        part100 = aqdata["particles 100um"]

        data = {}
        data.update({'time':tx})
        data.update({'CO2':co2})
        data.update({'temp.c':bme280.temperature, 'temp':ftemp, 'humidity':bme280.humidity})
        data.update(aqdata)

        parts1 = f'.3μm:{part3} .5μm:{part5} 1μm:{part10}'
        parts2 = f'2.5μm:{part25} 5μm:{part50} 10μm:{part100}'
        logging.debug(f'{ttime} {tempstr} {pmstd} {parts1} {parts2}')
        values = [tday, ttime, ftemp, bme280.humidity, pm10std, pm25std, pm100std, part3, part5, part10, part25, part50, part100, hostname, co2]
        lines = [f'{tday} {ttime}',f'{hostname} ({ip})',tempstr,pmstd]

        if ((logged_time is None) or ((time.mktime(tx)-time.mktime(logged_time)) > args.interval)):
            logging.info(f'{ttime} logging to gsheet every {args.interval}s and local data log')
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

        if not (buttonA.value):
            screen = True
            backlight(True)
        if not (buttonB.value):
            screen = False
            backlight(False)
        if not (buttonA.value) and not (buttonB.value): #gtfo
            break

    scd4x.stop_periodic_measurement()

if __name__=='__main__':
    main()
