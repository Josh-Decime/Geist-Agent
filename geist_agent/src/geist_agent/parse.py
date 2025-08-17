import argparse

def parse_args():
    """
    Parse command-line arguments using subparsers for function-specific options.
    """
    parser = argparse.ArgumentParser(
        description="AI Agent CLI for running workflows",
        epilog="Example: python poltern.py scry --topic 'Seances'"
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Function to run")

    # Subparser for 'scry'
    scry_parser = subparsers.add_parser("scry", help="Run scrying operations")
    scry_parser.add_argument(
        "--topic",
        type=str,
        default="The Meaning of Life",
        help="Topic for the scry function (default: The Meaning of Life)"
    )

    return parser.parse_args()