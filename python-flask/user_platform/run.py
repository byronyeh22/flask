import sys
import os

# Add the current directory to the Python path so that local modules can be imported
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Import the Flask application factory function
from app import create_app

# Create the Flask app instance using the factory pattern
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=16999, debug=True)
