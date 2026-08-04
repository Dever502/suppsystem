#!/bin/sh
set -eu

sh scripts/check_quality.sh
sh scripts/test_unit.sh
