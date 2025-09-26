declare global {
  interface Window {
    Telegram?: {
      WebApp: {
        initData: string;
        initDataUnsafe: {
          user?: {
            id: number;
            username?: string;
            first_name?: string;
            last_name?: string;
          };
        };
        ready: () => void;
        expand: () => void;
        close: () => void;
        MainButton: {
          text: string;
          color: string;
          textColor: string;
          isVisible: boolean;
          isActive: boolean;
          show: () => void;
          hide: () => void;
          enable: () => void;
          disable: () => void;
          onClick: (callback: () => void) => void;
        };
        BackButton: {
          isVisible: boolean;
          show: () => void;
          hide: () => void;
          onClick: (callback: () => void) => void;
        };
        themeParams: any; // Added for getTelegramThemeParams
        onEvent: (event: string, callback: () => void) => void; // Added for subscribeThemeChanged
      };
    };
  }
}

export const initTelegramApp = () => {
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }
};

export const getTelegramUser = () => {
  return window.Telegram?.WebApp?.initDataUnsafe?.user;
};

export const closeTelegramApp = () => {
  window.Telegram?.WebApp?.close();
};

export const getTelegramThemeParams = () => {
  // themeParams may live under window.Telegram.WebApp.themeParams
  return window.Telegram?.WebApp?.themeParams || null;
};

export const subscribeThemeChanged = (callback: (themeParams: any) => void) => {
  // Telegram WebApp exposes a theme_changed event via window.Telegram.WebApp.onEvent
  const tg = window.Telegram?.WebApp;
  if (tg && typeof tg.onEvent === 'function') {
    tg.onEvent('themeChanged', () => {
      callback(tg.themeParams);
    });
  }
}; 
