# SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
# Copyright (c) 2021-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

FROM python:3.10-slim

WORKDIR /app

# Copy requirements
COPY tests/dummy_robot/requirements.txt /app/requirements.txt

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy necessary files
COPY cloud_common /app/cloud_common
COPY packages/controllers/mission/vda5050_types /app/packages/controllers/mission/vda5050_types
COPY tests/dummy_robot/dummy_robot.py /app/tests/dummy_robot/dummy_robot.py

# Create __init__.py files for proper imports
RUN touch /app/cloud_common/__init__.py && \
    mkdir -p /app/packages/controllers/mission && \
    touch /app/packages/__init__.py && \
    touch /app/packages/controllers/__init__.py && \
    touch /app/packages/controllers/mission/__init__.py

# Set Python path
ENV PYTHONPATH=/app

# Run the dummy robot
CMD ["python", "tests/dummy_robot/dummy_robot.py"]

