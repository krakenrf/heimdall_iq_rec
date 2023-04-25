#!/bin/sh

# Remove old logfiles
rm _logs/*.log

# Start IQ rec
sudo python3 _src/IQStreamer.py 2> _logs/iq_recorder.log
