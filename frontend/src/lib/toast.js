import { notifications } from '@mantine/notifications';
import { errorMessage } from '../api/client';

export const toast = {
  ok: (message, title) => notifications.show({ title, message, color: 'teal', autoClose: 4000 }),
  info: (message, title) => notifications.show({ title, message, color: 'blue', autoClose: 5000 }),
  err: (e, title = 'Not saved') => notifications.show({ title, message: errorMessage(e), color: 'red', autoClose: 8000 }),
};
