import sys

from . import toes


def main():
    args = sys.argv[1:]
    if not args:
        from .browser import main as browser_main
        browser_main()
        return
    flag = args[0]
    if flag == "--toes":
        toes.list_toes()
        return
    if flag == "--toe-search":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --toe-search <term>")
            sys.exit(1)
        toes.search_toes(args[1])
        return
    if flag == "--toe-install":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --toe-install <name>")
            sys.exit(1)
        toes.install_toe(args[1])
        return
    if flag == "--toe-uninstall":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --toe-uninstall <name>")
            sys.exit(1)
        toes.uninstall_toe(args[1])
        return
    if flag == "--toe-enable" or flag == "--toe-disable":
        if len(args) < 2:
            print(f"usage: python3 -m feetbrowser {flag} <name>")
            sys.exit(1)
        sys.exit(toes.set_enabled(args[1], flag == "--toe-enable"))
    if flag == "--new-toe":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --new-toe <name>")
            sys.exit(1)
        sys.exit(toes.new_toe(args[1]))
    if flag == "--toe-docs":
        toes.toe_docs()
        return
    # Anything else is a URL passed to the browser.
    from .browser import main as browser_main
    browser_main()


if __name__ == "__main__":
    main()
