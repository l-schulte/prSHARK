#!/bin/bash

current=`pwd`
mkdir -p /tmp/reviewSHARK/
cp * /tmp/reviewSHARK/
cp -R ../reviewSHARK /tmp/reviewSHARK/
cp ../setup.py /tmp/reviewSHARK/
cp ../smartshark_plugin.py /tmp/reviewSHARK/
cd /tmp/reviewSHARK/

tar -cvf "$current/reviewSHARK_plugin.tar" --exclude=*.tar --exclude=build_plugin.sh --exclude=*/tests --exclude=*/__pycache__ --exclude=*.pyc *
