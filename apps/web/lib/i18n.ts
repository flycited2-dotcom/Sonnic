import ru from "../messages/ru.json";
import en from "../messages/en.json";
import type { Locale } from "./api";
type Messages = typeof ru;
const english: Messages = en;
export const messages: Record<Locale, Messages> = { "ru-RU": ru, "en-US": english };
