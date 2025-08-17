# Geist Agent
This is a custom AI Agent built off the CrewAI template. It implements a custom argparse CLI instead of the traditional CrewAI run command, to allow for a wider range of functionality.

# Current CLI Commands 
After activating your virtual environment, run from the src/geist_agent directory:
- python poltern.py --help # Show available commands
- python poltern.py scry # Run scry with default topic
- python poltern.py scry --topic 'Your Custom Topic' # Run scry with a custom topic
- python poltern.py scry --help # Show scry-specific options