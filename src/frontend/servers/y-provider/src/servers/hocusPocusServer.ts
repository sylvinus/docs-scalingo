import { Server, ConnectionConfiguration } from '@hocuspocus/server';

import jwt from 'jsonwebtoken';

import { logger } from '@/utils';
import { COLLABORATION_SERVER_SECRET } from '@/env';

type DecodedToken = {
  can_edit: boolean;
  user_id: string;
  document_id: string;
  exp?: number;
}

const validateAuth = (decoded: DecodedToken, connection: ConnectionConfiguration, documentName: string) => {
  if (decoded.document_id !== documentName) {
    console.error(
      'Invalid room name - Probable hacking attempt:',
      documentName,
      decoded.document_id,
      decoded.user_id,
    );
    throw new Error("Invalid document name");
  }
  connection.readOnly = !decoded.can_edit;

  logger(
    'Connection established:',
    documentName,
    'userId:',
    decoded.user_id,
    'canEdit:',
    decoded.can_edit,
    'room:',
    documentName,
  );

  return {
    user_id: decoded.user_id,
    document_id: decoded.document_id,
  };

}

export const hocusPocusServer = Server.configure({
  name: 'docs-collaboration',
  timeout: 30000,
  quiet: true,
  async onAuthenticate({documentName, token, connection}) {
    try {
      const decoded = jwt.verify(token, process.env.COLLABORATION_SERVER_SECRET as string) as unknown as DecodedToken;
      return validateAuth(decoded, connection, documentName);
    } catch {
      throw new Error("Not authorized!");
    }
  },
  async onConnect({ requestHeaders, connection, documentName, requestParameters }) {
    const roomParam = requestParameters.get('room');
    const apiKey = requestHeaders['authorization'];

    if (apiKey) {
      // Secret API Key check
      if (apiKey !== COLLABORATION_SERVER_SECRET) {
        throw new Error('Invalid API Key');
      }
      connection.requiresAuthentication = false;
      return validateAuth({
        can_edit: requestHeaders['x-can-edit'] === 'True',
        user_id: requestHeaders['x-user-id'] as string,
        document_id: roomParam as string,
      }, connection, documentName);
    }

  },
});
