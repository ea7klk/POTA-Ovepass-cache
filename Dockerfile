# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 5005 available to the world outside this container
EXPOSE 5005

# Run gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5005", "--workers", "4", "--threads", "4", "wsgi:app"]