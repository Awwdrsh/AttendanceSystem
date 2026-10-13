import { notifications } from '@mantine/notifications';
import { errorMessage } from '../api/client';

export const toast = {
  ok: (message: string, title?: string) => notifications.show({ title, message, color: 'teal', autoClose: 4000 }),
  info: (message: string, title?: string) => notifications.show({ title, message, color: 'blue', autoClose: 5000 }),
  err: (e: unknown, title = 'Not saved') => notifications.show({ title, message: errorMessage(e), color: 'red', autoClose: 8000 }),
};
