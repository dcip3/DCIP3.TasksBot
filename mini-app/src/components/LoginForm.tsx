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
    <div className="min-h-screen flex items-center justify-center bg-telegram-secondary px-4">
      <div className="max-w-md w-full space-y-8">
        <div>
          <h2 className="mt-6 text-center text-3xl font-extrabold text-telegram-primary">
            Вход в TasksBot
          </h2>
          <p className="mt-2 text-center text-sm text-telegram-gray">
            Введите ваши учетные данные Deadline
          </p>
        </div>
        <form className="mt-8 space-y-6" onSubmit={handleSubmit}>
          <div className="space-y-4">
            <div>
              <label htmlFor="username" className="sr-only">
                Логин
              </label>
              <div className="relative">
                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                  <User className="h-5 w-5 text-telegram-gray" />
                </div>
                <input
                  id="username"
                  name="username"
                  type="text"
                  required
                  className="appearance-none rounded-lg relative block w-full px-3 py-2 pl-10 border border-telegram-secondary placeholder-telegram-gray text-telegram-dark focus:outline-none focus:ring-2 focus:ring-telegram-primary focus:border-telegram-primary focus:z-10 sm:text-sm"
                  placeholder="Логин Deadline"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  disabled={loading}
                />
              </div>
            </div>
            <div>
              <label htmlFor="password" className="sr-only">
                Пароль
              </label>
              <div className="relative">
                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                  <Lock className="h-5 w-5 text-telegram-gray" />
                </div>
                <input
                  id="password"
                  name="password"
                  type="password"
                  required
                  className="appearance-none rounded-lg relative block w-full px-3 py-2 pl-10 border border-telegram-secondary placeholder-telegram-gray text-telegram-dark focus:outline-none focus:ring-2 focus:ring-telegram-primary focus:border-telegram-primary focus:z-10 sm:text-sm"
                  placeholder="Пароль Deadline"
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
              className="group relative w-full flex justify-center py-2 px-4 border border-transparent text-sm font-medium rounded-lg text-telegram-white bg-telegram-primary hover:bg-telegram-accent focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-telegram-primary disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? (
                <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-telegram-white"></div>
              ) : (
                'Войти'
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}; 