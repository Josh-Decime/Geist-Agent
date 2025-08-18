# Once Typer is fully working this file can be deleted

import argparse

def parse_args():
    """
    Parse command-line arguments using subparsers for function-specific options.
    """
    parser = argparse.ArgumentParser(
        description="commands:\n"
                    "  scry        Directs Geist to research a topic",
        epilog="Examples:\n"
               "  python poltern.py scry --help                       # Show scry-specific options",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False  # Move -h/--help to options section
    )
    parser.add_argument(
        "-h", "--help",
        action="help",
        help="show this help message and exit"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
        help=argparse.SUPPRESS  # Suppress subparser help to avoid positional arguments section
    )

    # Subparser for 'scry'
    scry_parser = subparsers.add_parser(
        "scry",
        description="Directs Geist to research a specified topic",
        epilog="Examples:\n"
               "  python poltern.py scry                              # Run scry with default topic (The Meaning of Life)\n"
               "  python poltern.py scry --topic 'Your Custom Topic'  # Run scry with a custom topic\n"
               "  python poltern.py --help                            # Show all available commands",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    scry_parser.add_argument(
        "--topic",
        type=str,
        default="The Meaning of Life",
        help="Input your custom topic (default: The Meaning of Life)"
    )

    return parser.parse_args()