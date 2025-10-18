# TasksBot Mini App

Telegram Mini App for managing Deadline jobs through a web interface.

## Features

- 🔐 Authentication with Deadline credentials
- 📋 Job list view with batch grouping
- 👥 Worker monitoring
- ⚡ Job controls: requeue, resume, suspend, delete
- 📊 Progress tracking for active jobs
- 🎨 Modern responsive UI
- 🔄 Automatic data refresh

## Setup

1. Install dependencies:
```bash
npm install
```

2. Create a `.env` file in the project root:
```env
VITE_API_URL=http://localhost:8000
```

3. Start the development server:
```bash
npm run dev
```

## Production Build

```bash
npm run build
```

## Telegram Bot Integration

Expose the following API endpoints in the bot backend for the mini app to function:

### Authentication
- `POST /api/auth/login` - user login
- `POST /api/auth/logout` - end the session
- `GET /api/auth/check` - session validation

### Jobs
- `GET /api/jobs` - fetch the job list
- `GET /api/jobs/{id}` - job details
- `GET /api/jobs/{id}/tasks` - tasks inside a job
- `PUT /api/jobs/{id}/requeue` - restart a job
- `PUT /api/jobs/{id}/resume` - resume a paused job
- `PUT /api/jobs/{id}/suspend` - pause a job
- `DELETE /api/jobs/{id}` - delete a job

### Workers
- `GET /api/workers` - fetch worker list

## Project Structure

```
mini-app/
├── src/
│   ├── components/     # React components
│   ├── services/       # API services
│   ├── types/          # TypeScript types
│   ├── utils/          # Shared utilities
│   ├── App.tsx         # Root component
│   └── main.tsx        # Entry point
├── public/             # Static assets
├── package.json        # Dependencies
└── README.md          # Documentation
```

## Technology Stack

- **React 18** - UI library
- **TypeScript** - type safety
- **Tailwind CSS** - styling
- **Vite** - build tool
- **Axios** - HTTP client
- **Lucide React** - icon set
- **Telegram WebApp API** - Telegram integration

## Development Tips

### Adding New Components

1. Create a file in `src/components/`
2. Export the component as a default or named export
3. Import it where it is needed

### Adding API Endpoints

1. Create a method in the appropriate service inside `src/services/api.ts`
2. Consume it from components via hooks or directly

### Styling Guidelines

Use Tailwind CSS utility classes for styling. Preferred color tokens:
- Primary: `primary-500`, `primary-600`, `primary-700`
- Success: `green-500`, `green-600`
- Warning: `yellow-500`, `yellow-600`
- Error: `red-500`, `red-600`
