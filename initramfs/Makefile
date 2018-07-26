#!/usr/bin/make -f

TESTFILES := $(shell find . | xargs file | grep shell | sed 's/:.*$$/ /g' | tr -d '\n')

check:
	@set -e; for f in $(TESTFILES); do \
	    echo "Checking shell syntax of $$f"; \
	    sh -n $$f; \
            shellcheck $$f; \
	done

