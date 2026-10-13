import { createContext, useContext } from 'react';

/** True when a page is rendered as a tab inside a hub; PageHeader then drops its own title. */
export const EmbeddedContext = createContext(false);
export const useEmbedded = (): boolean => useContext(EmbeddedContext);
