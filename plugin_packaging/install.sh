#!/bin/sh
PLUGIN_PATH=$1
cd $PLUGIN_PATH

python3 -m pip install --user --no-cache-dir --upgrade --upgrade-strategy only-if-needed "pycoshark @ git+https://github.com/smartshark/pycoSHARK.git@2.0.0"
python3 $PLUGIN_PATH/setup.py install --user
