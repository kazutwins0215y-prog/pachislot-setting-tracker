import os
import sys

FASE1_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'fase1'))
if FASE1_DIR not in sys.path:
    sys.path.insert(0, FASE1_DIR)

FASE2_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'fase2'))
if FASE2_DIR not in sys.path:
    sys.path.insert(0, FASE2_DIR)

FASE4_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'fase4'))
if FASE4_DIR not in sys.path:
    sys.path.insert(0, FASE4_DIR)
