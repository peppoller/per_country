#!/usr/bin/env bash
log_file=$(basename "$0" .sh).log
act schedule --container-architecture linux/amd64 |
  tee "$log_file"
