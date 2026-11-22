#!/bin/sh
case "$1" in
  *Username*|*username*) printf '%s\n' "${GTM_GIT_USERNAME:-sync}" ;;
  *Password*|*password*) printf '%s\n' "$GTM_GIT_PASSWORD" ;;
  *) exit 1 ;;
esac
