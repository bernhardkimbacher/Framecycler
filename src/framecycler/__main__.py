try:
    from .main import main
except ImportError:
    import sys
    import os
    # Add parent folder of framecycler (src) to sys.path to enable absolute imports in frozen environments
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from framecycler.main import main

if __name__ == "__main__":
    main()
