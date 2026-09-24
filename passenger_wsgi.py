import sys
import os

# Add the project root to path
sys.path.insert(0, os.path.dirname(__file__))

from app import app as application
