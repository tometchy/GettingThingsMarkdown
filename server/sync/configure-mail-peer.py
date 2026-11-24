#!/usr/bin/env python3
# Separate Syncthing identity for public-key encrypted mailbox bytes only.
import sys, xml.etree.ElementTree as ET
path = sys.argv[1]
tree = ET.parse(path)
root = tree.getroot()
for folder in root.findall("folder"):
    if folder.get("id") == "default":
        root.remove(folder)
options = root.find("options")
for element in list(options.findall("listenAddress")):
    options.remove(element)
ET.SubElement(options, "listenAddress").text = "tcp://0.0.0.0:22001"
for name in ("localAnnounceEnabled", "globalAnnounceEnabled", "relaysEnabled"):
    element = options.find(name)
    if element is None: element = ET.SubElement(options, name)
    element.text = "false"
tree.write(path, encoding="utf-8", xml_declaration=True)
