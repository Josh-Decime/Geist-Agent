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
        'topic': 'Ghost Sightings',
        'current_year': str(datetime.now().year)
    }
    try:
        ScryingAgent().scrying().kickoff(inputs=inputs)
    except Exception as e:
        raise Exception(f"An error occurred while running the scrying: {e}")


if __name__ == "__main__":
    scry()