/** Handshake reply for an approved device (plan Appendix B). Stamps are server-side cursors. */
export function buildOptions(sn: string, o: { attlogStamp: string; oplogStamp: string; serverVersion: string }): string {
  return [
    `GET OPTION FROM: ${sn}`,
    `Stamp=${o.attlogStamp}`,
    `OpStamp=${o.oplogStamp}`,
    `PhotoStamp=0`,
    `ATTLOGStamp=${o.attlogStamp}`,
    `OPERLOGStamp=${o.oplogStamp}`,
    `ATTPHOTOStamp=0`,
    'ErrorDelay=60',
    'Delay=10',
    'TransTimes=00:00;14:05',
    'TransInterval=1',
    'TransFlag=1111000000',
    'Realtime=1',
    'Encrypt=0',
    `ServerVer=${o.serverVersion}`,
    '',
  ].join('\n');
}
