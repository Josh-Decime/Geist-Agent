#!/usr/bin/env python
import sys
import warnings
from datetime import datetime

from geist_agent.scrying import ScryingAgent

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")


def scry():
    """
    Run the custom crew for scrying operations.
    """
    inputs = {
        'topic': 'AI in Cybersecurity',
        'current_year': str(datetime.now().year)
    }
    try:
        scrying_agent = ScryingAgent()
        scrying_agent.set_topic(inputs['topic'])
        scrying_agent.scrying().kickoff(inputs=inputs)
    except Exception as e:
        raise Exception(f"An error occurred while running the scrying crew: {e}")


if __name__ == "__main__":
    scry()