import React, { useState } from 'react';
import { User, Lock } from 'lucide-react';

interface LoginFormProps {
  onLogin: (username: string, password: string) => void;
  loading?: boolean;
}

export const LoginForm: React.FC<LoginFormProps> = ({ onLogin, loading = false }) => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (username.trim() && password.trim()) {
      onLogin(username.trim(), password);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-tg-bg px-4">
      <div className="max-w-md w-full space-y-8">
        <div>
          <h2 className="mt-6 text-center text-3xl font-extrabold text-tg-button">
            TasksBot Login
          </h2>
          <p className="mt-2 text-center text-sm text-tg-subtitle">
            Enter your Deadline credentials
          </p>
        </div>
        <form className="mt-8 space-y-6" onSubmit={handleSubmit}>
          <div className="space-y-4">
            <div>
              <label htmlFor="username" className="sr-only">
                Username
              </label>
              <div className="relative">
                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                  <User className="h-5 w-5 text-tg-hint" />
                </div>
                <input
                  id="username"
                  name="username"
                  type="text"
                  required
                  className="appearance-none rounded-lg relative block w-full px-3 py-2 pl-10 border border-tg-secondary-bg placeholder-tg-hint text-tg-base focus:outline-none focus:ring-2 focus:ring-tg-button focus:border-tg-button focus:z-10 sm:text-sm bg-tg-secondary-bg"
                  placeholder="Deadline Username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  disabled={loading}
                />
              </div>
            </div>
            <div>
              <label htmlFor="password" className="sr-only">
                Password
              </label>
              <div className="relative">
                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                  <Lock className="h-5 w-5 text-tg-hint" />
                </div>
                <input
                  id="password"
                  name="password"
                  type="password"
                  required
                  className="appearance-none rounded-lg relative block w-full px-3 py-2 pl-10 border border-tg-secondary-bg placeholder-tg-hint text-tg-base focus:outline-none focus:ring-2 focus:ring-tg-button focus:border-tg-button focus:z-10 sm:text-sm bg-tg-secondary-bg"
                  placeholder="Deadline Password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={loading}
                />
              </div>
            </div>
          </div>
          <div>
            <button
              type="submit"
              disabled={loading || !username.trim() || !password.trim()}
              className="group relative w-full flex justify-center py-2 px-4 border border-transparent text-sm font-medium rounded-lg text-tg-button-text bg-tg-button hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-tg-button disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
            >
              {loading ? (
                <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-tg-button-text"></div>
              ) : (
                'Login'
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}; 