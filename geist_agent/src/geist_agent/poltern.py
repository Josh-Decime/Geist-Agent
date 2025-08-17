#!/usr/bin/env python
import sys
import warnings
from datetime import datetime
from parse import parse_args

from geist_agent.scrying import ScryingAgent

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")

def scry(args):
    """
    Run the custom crew for scrying operations.
    """
    inputs = {
        'topic': args.topic,
        'current_year': str(datetime.now().year)
    }
    try:
        scrying_agent = ScryingAgent()
        scrying_agent.set_topic(inputs['topic'])
        scrying_agent.scrying().kickoff(inputs=inputs)
    except Exception as e:
        raise Exception(f"An error occurred while running the scrying crew: {e}")

if __name__ == "__main__":
    args = parse_args()

    if args.command == "scry":
        scry(args)
    else:
        print(f"Error: Unknown command '{args.command}'. Available commands: scry")
        print("Run 'python poltern.py --help' for usage.")
        sys.exit(1)