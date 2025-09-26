FROM node:18-alpine

# Set working directory
WORKDIR /app

# Copy package manifest files
COPY package*.json ./

# Install dependencies (including dev packages for build)
RUN npm ci

# Copy source code
COPY . .

# Build production assets
RUN npm run build

# Install serve for static hosting
RUN npm install -g serve

# Remove dev dependencies to shrink image size
RUN npm prune --production

# Expose web app port
EXPOSE 3000

# Container entrypoint
CMD ["serve", "-s", "dist", "-l", "3000"] 
