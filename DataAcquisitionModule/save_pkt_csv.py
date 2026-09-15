import serial
import csv
import datetime
import time

PORT = "/dev/ttyUSB0"
BAUD = 115200
LABEL = "attacco"          # tipo di traffico
OUTPUT_FILE = "dataset.csv"
counter = 0

ser = serial.Serial(PORT, BAUD, timeout=1)
print("Cattura avviata!")
with open(OUTPUT_FILE, "a", newline="") as f:
    writer = csv.writer(f)
    while True:
        counter += 1
        if((counter % 100) ==0):
            print(counter)
        if(counter == 14000):
            break
        line = ser.readline().decode(errors="ignore").strip()
        if not line or line.count(",") != 3:
            continue                     # salta righe vuote o non-CSV (es. il messaggio di avvio)
        src_ip, dst_ip, protocol, length = line.split(",")
        timestamp = datetime.datetime.now().isoformat(timespec='milliseconds')
        writer.writerow([timestamp, src_ip, dst_ip, protocol, length, LABEL])
        print(line)                      # feedback a schermo mentre cattura
